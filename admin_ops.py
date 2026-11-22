"""
admin_ops.py - admin changes to hospitals, doctors, patient requests and users (Phase III).

Every change is ONE transaction that
  1. locks the row it changes with SELECT ... FOR UPDATE,
  2. checks the business rules (e.g. a doctor with open cases cannot be deleted),
  3. writes the change and an audit-log row, then COMMITs (or ROLLBACKs).
So an admin edit can never interleave badly with a dispatch that is booking the
same doctor or unit at the same moment. Deadlocks / lock timeouts are retried by
transaction_manager._with_retry, exactly like reservations.

Which user may call what is decided in app.py (auth.require_role / check_hospital_access).
"""
import pymysql

import auth
import db
from transaction_manager import TxError, _begin, _with_retry

SPECIALIZATIONS = ["CARDIOLOGIST", "NEUROLOGIST", "TRAUMA_SURGEON", "PULMONOLOGIST",
                   "BURN_SPECIALIST", "GYNECOLOGIST", "GENERAL_PHYSICIAN"]
RESOURCE_TYPES = ["ICU_BED", "VENTILATOR", "OPERATION_THEATRE", "GENERAL_BED"]


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------
def _text(value, field, max_len, required=True):
    v = str(value if value is not None else "").strip()
    if required and not v:
        raise TxError(f"{field} is required")
    if len(v) > max_len:
        raise TxError(f"{field} is too long (max {max_len} characters)")
    return v or None


def _int(value, field, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise TxError(f"{field} must be a whole number")
    if not lo <= v <= hi:
        raise TxError(f"{field} must be between {lo} and {hi}")
    return v


def _coord(value, field, limit):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise TxError(f"{field} must be a number")
    if not -limit <= v <= limit:
        raise TxError(f"{field} is out of range")
    return v


def _spec(value):
    v = str(value or "").upper()
    if v not in SPECIALIZATIONS:
        raise TxError(f"Specialization must be one of {', '.join(SPECIALIZATIONS)}")
    return v


def _hospital_fields(d):
    return {
        "name": _text(d.get("name"), "Hospital name", 120),
        "area": _text(d.get("area"), "Area", 80),
        "phone": _text(d.get("phone"), "Phone", 20, required=False),
        "latitude": _coord(d.get("latitude"), "Latitude", 90),
        "longitude": _coord(d.get("longitude"), "Longitude", 180),
    }


def _duplicate(e):
    return isinstance(e, pymysql.err.IntegrityError) and e.args and e.args[0] == 1062


# ---------------------------------------------------------------------------
# Hospitals
# ---------------------------------------------------------------------------
def _try_add_hospital(conn, f, units):
    _begin(conn)
    with conn.cursor() as cur:
        try:
            cur.execute("INSERT INTO hospitals (name, area, latitude, longitude, phone) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (f["name"], f["area"], f["latitude"], f["longitude"], f["phone"]))
        except pymysql.err.IntegrityError as e:
            conn.rollback()
            if _duplicate(e):
                raise TxError(f"A hospital called '{f['name']}' already exists")
            raise
        hid = cur.lastrowid
        for rtype in RESOURCE_TYPES:
            total, free = units.get(rtype, (0, 0))
            cur.execute("INSERT INTO resources (hospital_id, resource_type, total_units, available_units) "
                        "VALUES (%s,%s,%s,%s)", (hid, rtype, total, free))
        db.log_event(cur, "ADMIN_UPDATE", f"Hospital added: {f['name']} ({f['area']})", None, hid)
    conn.commit()
    return {"result": "created", "hospital_id": hid}


def add_hospital(d):
    f = _hospital_fields(d)
    units = {}
    for rtype in RESOURCE_TYPES:
        total = _int((d.get("units") or {}).get(rtype, {}).get("total", 0), f"{rtype} total", 0, 5000)
        free = _int((d.get("units") or {}).get(rtype, {}).get("free", total), f"{rtype} free", 0, 5000)
        if free > total:
            raise TxError(f"{rtype}: free units cannot be more than total units")
        units[rtype] = (total, free)
    return _with_retry(_try_add_hospital, f, units)


def _try_update_hospital(conn, hid, f):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM hospitals WHERE hospital_id = %s AND is_lab = 0 FOR UPDATE", (hid,))
        old = cur.fetchone()
        if not old:
            conn.rollback()
            return {"result": "not_found"}
        try:
            cur.execute("UPDATE hospitals SET name=%s, area=%s, phone=%s, latitude=%s, longitude=%s "
                        "WHERE hospital_id = %s",
                        (f["name"], f["area"], f["phone"], f["latitude"], f["longitude"], hid))
        except pymysql.err.IntegrityError as e:
            conn.rollback()
            if _duplicate(e):
                raise TxError(f"A hospital called '{f['name']}' already exists")
            raise
        db.log_event(cur, "ADMIN_UPDATE", f"Hospital details changed: {old['name']} -> {f['name']}, "
                                          f"{f['area']}, {f['phone'] or 'no phone'}", None, hid)
    conn.commit()
    return {"result": "updated"}


def update_hospital(hid, d):
    return _with_retry(_try_update_hospital, int(hid), _hospital_fields(d))


def _try_delete_hospital(conn, hid):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM hospitals WHERE hospital_id = %s AND is_lab = 0 FOR UPDATE", (hid,))
        h = cur.fetchone()
        if not h:
            conn.rollback()
            return {"result": "not_found"}
        cur.execute("SELECT COUNT(*) AS n FROM allocations WHERE hospital_id = %s", (hid,))
        if cur.fetchone()["n"]:
            conn.rollback()
            raise TxError(f"{h['name']} has patient records, so it cannot be deleted "
                          "(that would erase case history). Set its free units to 0 instead.")
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE hospital_id = %s", (hid,))
        if cur.fetchone()["n"]:
            conn.rollback()
            raise TxError(f"{h['name']} still has admin accounts. Remove them first.")
        cur.execute("DELETE FROM hospitals WHERE hospital_id = %s", (hid,))   # resources+doctors cascade
        db.log_event(cur, "ADMIN_UPDATE", f"Hospital deleted: {h['name']}", None, None)
    conn.commit()
    return {"result": "deleted"}


def delete_hospital(hid):
    return _with_retry(_try_delete_hospital, int(hid))


# ---------------------------------------------------------------------------
# Doctors
# ---------------------------------------------------------------------------
def _doctor_fields(d):
    return {
        "name": _text(d.get("name"), "Doctor name", 80),
        "specialization": _spec(d.get("specialization")),
        "max_cases": _int(d.get("max_cases", 3), "Max cases", 1, 50),
        "on_shift": 1 if d.get("on_shift", True) in (True, 1, "1", "true", "on") else 0,
    }


def _try_add_doctor(conn, hid, f):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM hospitals WHERE hospital_id = %s AND is_lab = 0 FOR UPDATE", (hid,))
        h = cur.fetchone()
        if not h:
            conn.rollback()
            return {"result": "not_found"}
        cur.execute("INSERT INTO doctors (hospital_id, name, specialization, on_shift, max_cases) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (hid, f["name"], f["specialization"], f["on_shift"], f["max_cases"]))
        did = cur.lastrowid
        db.log_event(cur, "ADMIN_UPDATE", f"Doctor added at {h['name']}: {f['name']} "
                                          f"({f['specialization']}, max {f['max_cases']} cases)", None, hid)
    conn.commit()
    return {"result": "created", "doctor_id": did}


def add_doctor(hid, d):
    return _with_retry(_try_add_doctor, int(hid), _doctor_fields(d))


def doctor_hospital(did):
    row = db.query_one("SELECT hospital_id FROM doctors WHERE doctor_id = %s", (did,))
    return row["hospital_id"] if row else None


def _try_update_doctor(conn, did, f):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT hospital_id, name, current_cases FROM doctors WHERE doctor_id = %s FOR UPDATE",
                    (did,))
        doc = cur.fetchone()
        if not doc:
            conn.rollback()
            return {"result": "not_found"}
        if f["max_cases"] < doc["current_cases"]:
            conn.rollback()
            raise TxError(f"{doc['name']} is handling {doc['current_cases']} cases right now; "
                          f"max cases cannot be lower than that")
        cur.execute("UPDATE doctors SET name=%s, specialization=%s, on_shift=%s, max_cases=%s "
                    "WHERE doctor_id = %s",
                    (f["name"], f["specialization"], f["on_shift"], f["max_cases"], did))
        db.log_event(cur, "ADMIN_UPDATE", f"Doctor updated: {f['name']} ({f['specialization']}, "
                                          f"max {f['max_cases']}, {'on' if f['on_shift'] else 'off'} shift)",
                     None, doc["hospital_id"])
    conn.commit()
    return {"result": "updated"}


def update_doctor(did, d):
    return _with_retry(_try_update_doctor, int(did), _doctor_fields(d))


def _try_delete_doctor(conn, did):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT hospital_id, name, current_cases FROM doctors WHERE doctor_id = %s FOR UPDATE",
                    (did,))
        doc = cur.fetchone()
        if not doc:
            conn.rollback()
            return {"result": "not_found"}
        if doc["current_cases"] > 0:
            conn.rollback()
            raise TxError(f"{doc['name']} still has {doc['current_cases']} open case(s). "
                          "Release them or take the doctor off shift instead.")
        cur.execute("DELETE FROM doctors WHERE doctor_id = %s", (did,))   # old allocations keep NULL
        db.log_event(cur, "ADMIN_UPDATE", f"Doctor removed: {doc['name']}", None, doc["hospital_id"])
    conn.commit()
    return {"result": "deleted"}


def delete_doctor(did):
    return _with_retry(_try_delete_doctor, int(did))


# ---------------------------------------------------------------------------
# Patient (SOS request) details
# ---------------------------------------------------------------------------
def _try_update_request(conn, rid, f):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM sos_requests WHERE request_id = %s AND is_lab = 0 FOR UPDATE", (rid,))
        if not cur.fetchone():
            conn.rollback()
            return {"result": "not_found"}
        cur.execute("UPDATE sos_requests SET patient_name=%s, phone=%s, description=%s WHERE request_id=%s",
                    (f["patient_name"], f["phone"], f["description"], rid))
        db.log_event(cur, "PATIENT_UPDATED", f"Request #{rid}: patient details changed "
                                             f"({f['patient_name']}, {f['phone'] or 'no phone'})", rid)
    conn.commit()
    return {"result": "updated"}


def update_request(rid, d):
    f = {"patient_name": _text(d.get("patient_name"), "Patient name", 80),
         "phone": _text(d.get("phone"), "Phone", 20, required=False),
         "description": _text(d.get("description"), "Description", 255, required=False)}
    return _with_retry(_try_update_request, int(rid), f)


def _try_cancel_request(conn, rid):
    _begin(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM sos_requests WHERE request_id = %s AND is_lab = 0 FOR UPDATE", (rid,))
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return {"result": "not_found"}
        if row["status"] != "PENDING":
            conn.rollback()
            raise TxError(f"Only waiting requests can be cancelled (this one is {row['status']})")
        cur.execute("UPDATE sos_requests SET status = 'CANCELLED' WHERE request_id = %s", (rid,))
        db.log_event(cur, "CANCELLED", f"Request #{rid} cancelled, removed from the queue", rid)
    conn.commit()
    return {"result": "cancelled"}


def cancel_request(rid):
    return _with_retry(_try_cancel_request, int(rid))


# ---------------------------------------------------------------------------
# Users (system admin only)
# ---------------------------------------------------------------------------
def list_users():
    return db.query_all("SELECT u.user_id, u.username, u.full_name, u.phone, u.role, u.hospital_id, "
                        "h.name AS hospital_name, u.is_active, u.created_at, u.last_login_at "
                        "FROM users u LEFT JOIN hospitals h ON h.hospital_id = u.hospital_id "
                        "ORDER BY u.role, u.username")


def create_user(d):
    role = str(d.get("role") or "").upper()
    hid = d.get("hospital_id") or None
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            if hid is not None:
                cur.execute("SELECT 1 FROM hospitals WHERE hospital_id = %s AND is_lab = 0", (int(hid),))
                if not cur.fetchone():
                    raise TxError("That hospital does not exist")
            try:
                uid = auth.create_user(cur, d.get("username"), d.get("password"), d.get("full_name"),
                                       role, int(hid) if hid else None,
                                       _text(d.get("phone"), "Phone", 20, required=False))
            except ValueError as e:
                raise TxError(str(e))
            except pymysql.err.IntegrityError as e:
                if _duplicate(e):
                    raise TxError("That username is already taken")
                raise
            db.log_event(cur, "USER_ADDED", f"User '{str(d.get('username')).lower()}' added as {role}")
        conn.commit()
        return {"result": "created", "user_id": uid}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_user_active(uid, active, acting_user_id):
    if int(uid) == int(acting_user_id) and not active:
        raise TxError("You cannot disable your own account")
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE user_id = %s FOR UPDATE", (uid,))
            u = cur.fetchone()
            if not u:
                conn.rollback()
                return {"result": "not_found"}
            cur.execute("UPDATE users SET is_active = %s WHERE user_id = %s", (1 if active else 0, uid))
            db.log_event(cur, "USER_UPDATED", f"User '{u['username']}' {'enabled' if active else 'disabled'}")
        conn.commit()
        return {"result": "updated"}
    finally:
        conn.close()
