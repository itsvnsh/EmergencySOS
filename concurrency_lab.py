"""
concurrency_lab.py - race-condition demonstration.

N patients (threads, each with its OWN database connection) try to book the
LAST ICU bed of the "Concurrency Lab Hospital" at the same moment.

  locked=False  the classic UNSAFE pattern, no transaction, no locks:
                    read available_units  ->  (gap)  ->  write available_units - 1
                Every client reads "1 free", so every client books it
                (lost update): 6 bookings for 1 bed, counter is wrong.

  locked=True   uses transaction_manager.reserve(): SELECT ... FOR UPDATE inside
                a REPEATABLE READ transaction. The first client locks the row,
                the others wait, then see 0 free and ROLL BACK.
                Result: exactly 1 booking, counter consistent.

delay_ms adds a deliberate pause between the check and the update. The real
gap is tiny, so without it the unsafe version only fails SOMETIMES. With the
delay it fails EVERY time. The locked version gives the same correct result
with or without the delay.

Run from the command line:   python concurrency_lab.py
"""
import threading
import time

import db
import transaction_manager as tm


def _lab_ids():
    row = db.query_one(
        "SELECT h.hospital_id, h.name, r.resource_id, r.total_units FROM hospitals h "
        "JOIN resources r ON r.hospital_id = h.hospital_id AND r.resource_type = 'ICU_BED' "
        "WHERE h.is_lab = 1 LIMIT 1")
    if not row:
        raise RuntimeError("Lab hospital missing - run `python init_db.py` first")
    return row


def reset_lab(beds=1):
    """Remove old lab data and put `beds` free ICU beds back."""
    lab = _lab_ids()
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM allocations WHERE hospital_id = %s", (lab["hospital_id"],))
            cur.execute("DELETE FROM sos_requests WHERE is_lab = 1")
            cur.execute("UPDATE resources SET total_units = %s, available_units = %s "
                        "WHERE resource_id = %s", (beds, beds, lab["resource_id"]))
            cur.execute("UPDATE doctors SET current_cases = 0 WHERE hospital_id = %s",
                        (lab["hospital_id"],))
        conn.commit()
    finally:
        conn.close()
    return lab


def _create_lab_requests(n):
    conn = db.get_connection()
    ids = []
    try:
        with conn.cursor() as cur:
            for i in range(1, n + 1):
                cur.execute(
                    "INSERT INTO sos_requests (patient_name, latitude, longitude, emergency_type, "
                    "severity, description, priority, required_resource, required_specialist, "
                    "est_handling_min, is_lab) VALUES (%s, 30.3165, 78.0322, 'CARDIAC', 5, "
                    "'Concurrency lab patient', 1, 'ICU_BED', 'CARDIOLOGIST', 13, 1)",
                    (f"Lab Patient {i}",))
                ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return ids


def _unsafe_book(conn, request_id, lab, delay_ms):
    """UNSAFE on purpose: separate auto-committed statements, plain reads, no locks."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT available_units FROM resources WHERE resource_id = %s",
                        (lab["resource_id"],))
            seen = cur.fetchone()["available_units"]          # check ...
            if seen <= 0:
                return {"result": "no_unit", "seen_free": seen}
            if delay_ms:
                time.sleep(delay_ms / 1000.0)                 # ... gap ...
            cur.execute("UPDATE resources SET available_units = %s WHERE resource_id = %s",
                        (seen - 1, lab["resource_id"]))       # ... act on a stale value
            cur.execute("INSERT INTO allocations (request_id, hospital_id, resource_id) "
                        "VALUES (%s, %s, %s)", (request_id, lab["hospital_id"], lab["resource_id"]))
            cur.execute("UPDATE sos_requests SET status = 'ALLOCATED' WHERE request_id = %s",
                        (request_id,))
            return {"result": "allocated", "seen_free": seen}
    finally:
        conn.close()


def run_race(clients=6, locked=True, delay_ms=50):
    clients = max(2, min(int(clients), 20))
    delay_ms = max(0, min(int(delay_ms), 500))
    lab = reset_lab(beds=1)
    request_ids = _create_lab_requests(clients)
    lab_candidate = {"hospital_id": lab["hospital_id"], "resource_id": lab["resource_id"],
                     "name": lab["name"], "rank": 1, "score": None, "distance_km": 0}

    barrier = threading.Barrier(clients)   # release every thread at the same instant
    results = [None] * clients
    t0 = time.perf_counter()

    def worker(i, rid):
        # unlocked clients connect BEFORE the barrier so they all reach the check together
        conn = None if locked else db.get_connection(autocommit=True)
        barrier.wait()
        start = time.perf_counter()
        try:
            if locked:
                out = tm.reserve(rid, ranked=[lab_candidate], delay_ms=delay_ms)
                outcome = "allocated" if out["status"] == "allocated" else "rolled_back"
            else:
                out = _unsafe_book(conn, rid, lab, delay_ms)
                outcome = out["result"]
        except Exception as e:          # report errors instead of crashing the lab
            outcome = f"error: {e}"
        results[i] = {"client": i + 1, "request_id": rid, "result": outcome,
                      "started_ms": round((start - t0) * 1000, 1),
                      "finished_ms": round((time.perf_counter() - t0) * 1000, 1)}

    threads = [threading.Thread(target=worker, args=(i, rid)) for i, rid in enumerate(request_ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    bookings = db.query_one("SELECT COUNT(*) AS n FROM allocations WHERE hospital_id = %s",
                            (lab["hospital_id"],))["n"]
    counter = db.query_one("SELECT available_units, total_units FROM resources WHERE resource_id = %s",
                           (lab["resource_id"],))
    consistent = bookings <= counter["total_units"] and \
        bookings + counter["available_units"] == counter["total_units"]

    mode = "LOCKED (SELECT ... FOR UPDATE)" if locked else "UNLOCKED (no transaction)"
    db.execute("INSERT INTO audit_log (event_type, hospital_id, details) VALUES ('LAB_RACE', %s, %s)",
               (lab["hospital_id"], f"{mode}: {clients} clients, 1 bed, delay {delay_ms} ms -> "
                                    f"{bookings} booking(s), counter {counter['available_units']}, "
                                    f"{'consistent' if consistent else 'INCONSISTENT'}"))
    return {
        "mode": "locked" if locked else "unlocked",
        "mode_label": mode,
        "clients": clients,
        "delay_ms": delay_ms,
        "beds": counter["total_units"],
        "bookings": bookings,
        "rolled_back": sum(1 for r in results if r["result"] != "allocated"),
        "final_available": counter["available_units"],
        "consistent": consistent,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "clients_detail": sorted(results, key=lambda r: r["finished_ms"]),
    }


if __name__ == "__main__":
    for locked in (False, True):
        r = run_race(6, locked=locked, delay_ms=50)
        print(f"{r['mode_label']:32} bookings={r['bookings']}  rolled back={r['rolled_back']}  "
              f"counter={r['final_available']}  consistent={r['consistent']}")
    reset_lab()
