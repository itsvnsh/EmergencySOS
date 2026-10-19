"""
transaction_manager.py - safe allocation of hospital units (DBMS core).

Every reservation is ONE transaction:

    SET TRANSACTION ISOLATION LEVEL REPEATABLE READ
    START TRANSACTION
      1. SELECT ... FROM sos_requests WHERE id = ?  FOR UPDATE   (lock the request)
      2. SELECT ... FROM resources    WHERE id = ?  FOR UPDATE   (lock the unit counter)
      3. SELECT ... FROM doctors      WHERE ...     FOR UPDATE   (lock one doctor)
      4. re-check: request still PENDING? unit still free?
      5. UPDATE counters, mark request ALLOCATED, INSERT allocation + audit row
    COMMIT                       (or ROLLBACK if any check fails)

Mutual exclusion: SELECT ... FOR UPDATE takes an exclusive row lock, so two
transactions can never both read "1 bed free" and both take it.

Deadlock avoidance: every transaction locks rows in the SAME order
(request -> resource -> doctor -> allocation). With one global order a
circular wait cannot form.

Stale ranking: the ranking is computed from a snapshot BEFORE the transaction.
If the chosen hospital filled up meanwhile, the locked re-check fails, we
ROLLBACK and fall through to the next ranked hospital.

Deadlocks (1213) and lock-wait timeouts (1205) are retried automatically.
"""
import random
import time

import pymysql

import config
import db
from ranking import rank_for_request


class TxError(Exception):
    pass


def _begin(conn):
    with conn.cursor() as cur:
        cur.execute(f"SET TRANSACTION ISOLATION LEVEL {config.ISOLATION_LEVEL}")
    conn.begin()


def _with_retry(fn, *args, **kwargs):
    """Run a transaction function, retrying on deadlock / lock-wait timeout."""
    last_error = None
    for attempt in range(1, config.MAX_TX_RETRIES + 1):
        conn = db.get_connection()
        try:
            result = fn(conn, *args, **kwargs)
            result["tx_attempts"] = attempt
            return result
        except pymysql.err.OperationalError as e:
            conn.rollback()
            code = e.args[0] if e.args else None
            if code in (db.ER_LOCK_DEADLOCK, db.ER_LOCK_WAIT_TIMEOUT):
                last_error = "deadlock" if code == db.ER_LOCK_DEADLOCK else "lock wait timeout"
                time.sleep(random.uniform(0.02, 0.08) * attempt)   # back off, then retry
                continue
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"result": "lock_error", "error": last_error, "tx_attempts": config.MAX_TX_RETRIES}


# ---------------------------------------------------------------------------
# Reservation
# ---------------------------------------------------------------------------
def _try_reserve(conn, request_id, cand, delay_ms=0):
    """One reservation attempt at ONE hospital. Commits or rolls back."""
    _begin(conn)
    with conn.cursor() as cur:
        # (1) lock the request
        cur.execute("SELECT request_id, status, required_specialist FROM sos_requests "
                    "WHERE request_id = %s FOR UPDATE", (request_id,))
        req = cur.fetchone()
        if not req:
            conn.rollback()
            return {"result": "not_found"}
        if req["status"] != "PENDING":
            conn.rollback()
            return {"result": "not_pending", "status": req["status"]}

        # (2) lock the unit counter and re-check availability
        cur.execute("SELECT resource_id, available_units FROM resources "
                    "WHERE resource_id = %s FOR UPDATE", (cand["resource_id"],))
        res = cur.fetchone()
        if delay_ms:                      # only used by the concurrency lab
            time.sleep(delay_ms / 1000.0)
        if not res or res["available_units"] <= 0:
            conn.rollback()
            return {"result": "no_unit"}

        # (3) lock one doctor: required specialist first, else a general physician
        doctor = None
        specs = [req["required_specialist"]]
        if req["required_specialist"] != "GENERAL_PHYSICIAN":
            specs.append("GENERAL_PHYSICIAN")
        for spec in specs:
            cur.execute("SELECT doctor_id, name, specialization FROM doctors "
                        "WHERE hospital_id = %s AND specialization = %s AND on_shift = 1 "
                        "AND current_cases < max_cases "
                        "ORDER BY current_cases, doctor_id LIMIT 1 FOR UPDATE",
                        (cand["hospital_id"], spec))
            doctor = cur.fetchone()
            if doctor:
                break

        # (4) all checks passed -> update everything inside the same transaction
        cur.execute("UPDATE resources SET available_units = available_units - 1 "
                    "WHERE resource_id = %s", (cand["resource_id"],))
        if doctor:
            cur.execute("UPDATE doctors SET current_cases = current_cases + 1 "
                        "WHERE doctor_id = %s", (doctor["doctor_id"],))
        cur.execute("UPDATE sos_requests SET status = 'ALLOCATED' WHERE request_id = %s",
                    (request_id,))
        cur.execute("INSERT INTO allocations (request_id, hospital_id, resource_id, doctor_id, "
                    "score, distance_km) VALUES (%s,%s,%s,%s,%s,%s)",
                    (request_id, cand["hospital_id"], cand["resource_id"],
                     doctor["doctor_id"] if doctor else None,
                     cand.get("score"), cand.get("distance_km")))
        allocation_id = cur.lastrowid
        doc_txt = f"{doctor['name']} ({doctor['specialization']})" if doctor else "no doctor free"
        db.log_event(cur, "ALLOCATED",
                     f"Request #{request_id} -> {cand.get('name', cand['hospital_id'])}, "
                     f"unit #{cand['resource_id']}, {doc_txt}",
                     request_id, cand["hospital_id"])
    conn.commit()
    return {"result": "allocated", "allocation_id": allocation_id,
            "doctor": doctor, "units_before": res["available_units"]}


def reserve(request_id, ranked=None, delay_ms=0):
    """
    Reserve a unit for a pending request, trying hospitals in ranked order.
    Returns a dict describing the outcome and every attempt made.
    """
    req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (request_id,))
    if not req:
        raise TxError(f"Request #{request_id} does not exist")
    if req["status"] != "PENDING":
        return {"status": "not_pending", "request_id": request_id,
                "message": f"Request is already {req['status']}", "attempts": []}

    if ranked is None:
        ranked = rank_for_request(req)

    attempts = []
    for cand in ranked:
        outcome = _with_retry(_try_reserve, request_id, cand, delay_ms)
        attempts.append({"hospital_id": cand["hospital_id"], "hospital": cand.get("name"),
                         "rank": cand.get("rank"), "score": cand.get("score"),
                         "result": outcome["result"], "tx_attempts": outcome.get("tx_attempts")})

        if outcome["result"] == "allocated":
            return {"status": "allocated", "request_id": request_id,
                    "allocation_id": outcome["allocation_id"],
                    "hospital": cand, "doctor": outcome["doctor"],
                    "fallbacks": len(attempts) - 1, "attempts": attempts}
        if outcome["result"] in ("not_pending", "not_found"):
            return {"status": outcome["result"], "request_id": request_id, "attempts": attempts,
                    "message": "Request was handled by another dispatcher"}
        # no_unit (stale ranking) or lock_error -> log it and try the next hospital
        db.execute("INSERT INTO audit_log (event_type, request_id, hospital_id, details) "
                   "VALUES ('ROLLBACK', %s, %s, %s)",
                   (request_id, cand["hospital_id"],
                    f"Request #{request_id}: {outcome['result']} at {cand.get('name')} "
                    f"- rolled back, trying next hospital"))

    db.execute("INSERT INTO audit_log (event_type, request_id, details) VALUES ('NO_CAPACITY', %s, %s)",
               (request_id, f"Request #{request_id}: no hospital has a free "
                            f"{req['required_resource']} - stays in the queue"))
    return {"status": "no_capacity", "request_id": request_id, "attempts": attempts,
            "message": f"No hospital currently has a free {req['required_resource']}. "
                       f"The request stays in the queue."}


# ---------------------------------------------------------------------------
# Release (case finished / patient discharged)
# ---------------------------------------------------------------------------
def _try_release(conn, allocation_id):
    with conn.cursor() as cur:
        cur.execute("SELECT request_id, resource_id, doctor_id FROM allocations "
                    "WHERE allocation_id = %s", (allocation_id,))
        ids = cur.fetchone()
    conn.commit()            # end the read-only lookup before starting the real transaction
    if not ids:
        return {"result": "not_found"}

    _begin(conn)
    with conn.cursor() as cur:
        # same global lock order as reserve(): request -> resource -> doctor -> allocation
        cur.execute("SELECT status FROM sos_requests WHERE request_id = %s FOR UPDATE",
                    (ids["request_id"],))
        cur.execute("SELECT available_units, total_units FROM resources "
                    "WHERE resource_id = %s FOR UPDATE", (ids["resource_id"],))
        if ids["doctor_id"]:
            cur.execute("SELECT current_cases FROM doctors WHERE doctor_id = %s FOR UPDATE",
                        (ids["doctor_id"],))
        cur.execute("SELECT status, hospital_id FROM allocations WHERE allocation_id = %s FOR UPDATE",
                    (allocation_id,))
        alloc = cur.fetchone()
        if alloc["status"] != "ACTIVE":
            conn.rollback()
            return {"result": "already_released"}

        cur.execute("UPDATE resources SET available_units = LEAST(total_units, available_units + 1) "
                    "WHERE resource_id = %s", (ids["resource_id"],))
        if ids["doctor_id"]:
            cur.execute("UPDATE doctors SET current_cases = GREATEST(0, current_cases - 1) "
                        "WHERE doctor_id = %s", (ids["doctor_id"],))
        cur.execute("UPDATE allocations SET status = 'RELEASED', released_at = NOW() "
                    "WHERE allocation_id = %s", (allocation_id,))
        cur.execute("UPDATE sos_requests SET status = 'COMPLETED' WHERE request_id = %s",
                    (ids["request_id"],))
        db.log_event(cur, "RELEASED",
                     f"Allocation #{allocation_id} released, unit returned to the pool",
                     ids["request_id"], alloc["hospital_id"])
    conn.commit()
    return {"result": "released"}


def release(allocation_id):
    return _with_retry(_try_release, allocation_id)


# ---------------------------------------------------------------------------
# Hospital admin updates (also transactional)
# ---------------------------------------------------------------------------
def _try_update_resource(conn, hospital_id, resource_type, total_units, available_units):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT resource_id, total_units, available_units FROM resources "
                    "WHERE hospital_id = %s AND resource_type = %s FOR UPDATE",
                    (hospital_id, resource_type))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return {"result": "not_found"}
        cur.execute("UPDATE resources SET total_units = %s, available_units = %s "
                    "WHERE resource_id = %s", (total_units, available_units, row["resource_id"]))
        db.log_event(cur, "ADMIN_UPDATE",
                     f"{resource_type}: total {row['total_units']}->{total_units}, "
                     f"free {row['available_units']}->{available_units}", None, hospital_id)
    conn.commit()
    return {"result": "updated"}


def update_resource(hospital_id, resource_type, total_units, available_units):
    total_units, available_units = int(total_units), int(available_units)
    if total_units < 0 or available_units < 0:
        raise TxError("Unit counts cannot be negative")
    if available_units > total_units:
        raise TxError("Free units cannot be more than total units")
    return _with_retry(_try_update_resource, hospital_id, resource_type,
                       total_units, available_units)


def _try_set_shift(conn, doctor_id, on_shift):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT hospital_id, name FROM doctors WHERE doctor_id = %s FOR UPDATE",
                    (doctor_id,))
        doc = cur.fetchone()
        if not doc:
            conn.rollback()
            return {"result": "not_found"}
        cur.execute("UPDATE doctors SET on_shift = %s WHERE doctor_id = %s",
                    (1 if on_shift else 0, doctor_id))
        db.log_event(cur, "ADMIN_UPDATE",
                     f"{doc['name']} is now {'ON' if on_shift else 'OFF'} shift",
                     None, doc["hospital_id"])
    conn.commit()
    return {"result": "updated"}


def set_doctor_shift(doctor_id, on_shift):
    return _with_retry(_try_set_shift, doctor_id, bool(on_shift))
