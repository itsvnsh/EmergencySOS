"""
db_objects.py - the views, triggers and stored procedures of schema_objects.sql,
for the console's "Database" tab and the tests.

  catalog()          every object: what it does (its comment), its SQL, installed or not
  view_rows(name)    SELECT * from one of OUR views (name checked against the catalog)
  status_history(id) the trigger-written state timeline of one request
  trigger_demo()     shows the state-machine trigger refusing an illegal change
  hospital_report(h) CALL sp_hospital_report -> its two result sets
"""
import re

import pymysql

import db
import init_db

ER_SIGNAL = 1644            # error code of SIGNAL SQLSTATE '45000' raised by a trigger
KINDS = {"VIEW": "views", "TRIGGER": "triggers", "PROCEDURE": "procedures"}
_CREATE = re.compile(r"^CREATE\s+(VIEW|TRIGGER|PROCEDURE)\s+(\w+)", re.I)


def _parse_source():
    """Readable source of every object, with the comment block written above it."""
    with open(init_db.OBJECTS_FILE, encoding="utf-8") as f:
        lines = f.read().splitlines()
    found = []
    for i, line in enumerate(lines):
        m = _CREATE.match(line.strip())
        if not m:
            continue
        kind, name = m.group(1).upper(), m.group(2)
        about, j = [], i - 1
        while j >= 0 and lines[j].strip().startswith("--"):
            text = lines[j].strip()[2:].strip()
            if set(text) - set("=-"):                   # skip ==== banner lines
                about.insert(0, text)
            j -= 1
        end = i
        stop = (lambda s: s.rstrip().endswith(";")) if kind == "VIEW" else (lambda s: s.strip() == "END$$")
        while end < len(lines) and not stop(lines[end]):
            end += 1
        sql = "\n".join(lines[i:end + 1]).rstrip()
        sql = sql[:-2] + ";" if sql.endswith("END$$") else sql
        found.append({"kind": kind, "name": name, "about": " ".join(about), "sql": sql})
    return found


def installed():
    """{'VIEW': {...names}, 'TRIGGER': {...}, 'PROCEDURE': {...}} actually present in MySQL."""
    out = {"VIEW": set(), "TRIGGER": set(), "PROCEDURE": set()}
    for r in db.query_all("SELECT table_name AS n FROM information_schema.VIEWS WHERE table_schema = DATABASE()"):
        out["VIEW"].add(r["n"])
    for r in db.query_all("SELECT trigger_name AS n FROM information_schema.TRIGGERS "
                          "WHERE trigger_schema = DATABASE()"):
        out["TRIGGER"].add(r["n"])
    for r in db.query_all("SELECT routine_name AS n FROM information_schema.ROUTINES "
                          "WHERE routine_schema = DATABASE() AND routine_type = 'PROCEDURE'"):
        out["PROCEDURE"].add(r["n"])
    return out


def triggers_installed():
    return bool(installed()["TRIGGER"])


def catalog():
    present = installed()
    out = {"views": [], "triggers": [], "procedures": []}
    for obj in _parse_source():
        obj["installed"] = obj["name"] in present[obj["kind"]]
        out[KINDS[obj["kind"]]].append(obj)
    missing = [t["name"] for t in out["triggers"] if not t["installed"]]
    out["triggers_installed"] = not missing
    out["help"] = init_db.TRIGGER_HELP if missing else None
    return out


def view_rows(name, limit=50):
    names = {o["name"] for o in _parse_source() if o["kind"] == "VIEW"} & installed()["VIEW"]
    if name not in names:                     # only our own views: the name goes into SQL text
        raise ValueError(f"Unknown view '{name}'")
    return db.query_all(f"SELECT * FROM `{name}` LIMIT %s", (int(limit),))


def status_history(request_id):
    return db.query_all("SELECT history_id, old_status, new_status, changed_at FROM request_status_history "
                        "WHERE request_id = %s ORDER BY history_id", (request_id,))


def trigger_demo():
    """Create a lab request, walk it PENDING -> ALLOCATED -> COMPLETED, then try the
    illegal COMPLETED -> PENDING. The trigger refuses it; the history shows every legal step."""
    if not triggers_installed():
        raise RuntimeError(init_db.TRIGGER_HELP)
    conn = db.pooled(autocommit=True)
    steps = []
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sos_requests (patient_name, latitude, longitude, emergency_type, severity, "
                        "description, priority, required_resource, required_specialist, est_handling_min, is_lab) "
                        "VALUES ('Trigger demo', 30.3165, 78.0322, 'GENERAL', 2, 'Database tab demo', 4, "
                        "'GENERAL_BED', 'GENERAL_PHYSICIAN', 5, 1)")
            rid = cur.lastrowid
            for new in ("ALLOCATED", "COMPLETED", "PENDING"):
                sql = f"UPDATE sos_requests SET status = '{new}' WHERE request_id = {rid}"
                try:
                    cur.execute("UPDATE sos_requests SET status = %s WHERE request_id = %s", (new, rid))
                    steps.append({"sql": sql, "ok": True, "message": "accepted"})
                except pymysql.err.OperationalError as e:
                    if not (e.args and e.args[0] == ER_SIGNAL):
                        raise
                    steps.append({"sql": sql, "ok": False, "message": e.args[1]})
            history = status_history(rid)
            cur.execute("DELETE FROM sos_requests WHERE request_id = %s", (rid,))   # history rows cascade
    finally:
        conn.close()
    return {"request_id": rid, "steps": steps, "history": history}


def hospital_report(hospital_id):
    """CALL sp_hospital_report: a procedure can return more than one result set."""
    conn = db.pooled(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("CALL sp_hospital_report(%s)", (int(hospital_id),))
            units = cur.fetchall()
            cur.nextset()
            doctors = cur.fetchall()
        return {"units": list(units), "doctors": list(doctors)}
    finally:
        conn.close()
