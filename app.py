"""
app.py - Flask REST API + web console for EmergencySOS.

Run:   python app.py      then open http://127.0.0.1:5000

API (all JSON):
  GET  /api/meta                          emergency types, policies, preset locations
  POST /api/sos                           create an SOS request (classified on arrival)
  GET  /api/queue?policy=PRIORITY         pending queue ordered by a scheduling policy
  GET  /api/rank/<request_id>             ranked hospitals for a request (preview)
  POST /api/dispatch                      serve the next request {policy} or {request_id}
  GET  /api/requests                      recent requests
  GET  /api/allocations?status=ACTIVE     allocations
  POST /api/allocations/<id>/release      finish a case, return the unit
  GET  /api/hospitals                     capacity dashboard data
  POST /api/admin/resource                change unit counts
  POST /api/admin/doctor/<id>/shift       toggle a doctor's shift
  POST /api/admin/reset                   reload the simulated data
  GET  /api/audit                         audit log
  POST /api/demo/seed                     add demo requests with staggered arrival times
  GET  /api/labs/scheduler/workload       the 12-request sample workload
  POST /api/labs/scheduler                compare scheduling policies on a workload
  POST /api/labs/race                     run the concurrency race
"""
import datetime as dt
import decimal
import json
import os

from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

import classifier
import concurrency_lab
import config
import db
import init_db
import ranking
import scheduler
import transaction_manager as tm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PRESET_LOCATIONS = [
    {"name": "Graphic Era University, Clement Town", "lat": 30.2683, "lon": 77.9947},
    {"name": "Clock Tower (Ghanta Ghar)", "lat": 30.3247, "lon": 78.0418},
    {"name": "ISBT Dehradun", "lat": 30.2887, "lon": 78.0010},
    {"name": "Rajpur Road", "lat": 30.3484, "lon": 78.0618},
    {"name": "Prem Nagar", "lat": 30.3340, "lon": 77.9590},
    {"name": "Doiwala", "lat": 30.1760, "lon": 78.1210},
    {"name": "Rishikesh (Triveni Ghat)", "lat": 30.1046, "lon": 78.2988},
]


class JSONProvider(DefaultJSONProvider):
    """Turn MySQL DECIMAL and DATETIME values into plain JSON."""
    @staticmethod
    def default(o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        if isinstance(o, (dt.datetime, dt.date)):
            return o.isoformat(sep=" ") if isinstance(o, dt.datetime) else o.isoformat()
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = JSONProvider(app)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({"ok": False, "error": e.message}), e.status


@app.errorhandler(tm.TxError)
@app.errorhandler(classifier.ClassificationError)
@app.errorhandler(ValueError)
def handle_value_error(e):
    return jsonify({"ok": False, "error": str(e)}), 400


@app.errorhandler(Exception)
def handle_unexpected(e):
    import pymysql
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"ok": False, "error": e.description}), e.code
    if isinstance(e, pymysql.err.OperationalError) and e.args and e.args[0] in (2003, 1045, 1049):
        return jsonify({"ok": False, "error": f"Database not reachable: {e.args[1]}. "
                                              f"Is MySQL running and did you run init_db.py?"}), 503
    app.logger.exception("Unexpected error")
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


def body():
    return request.get_json(silent=True) or {}


def get_policy(value):
    policy = (value or "PRIORITY").upper()
    if policy not in scheduler.POLICIES:
        raise ApiError(f"Unknown policy '{policy}'. Use one of {scheduler.POLICIES}")
    return policy


def pending_requests():
    return db.query_all("SELECT * FROM sos_requests WHERE status = 'PENDING' AND is_lab = 0")


def db_now():
    """Use the database clock for waiting times (avoids PC/DB timezone mismatch)."""
    return db.query_one("SELECT NOW() AS now")["now"]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/meta")
def meta():
    return jsonify({
        "ok": True,
        "emergency_types": classifier.EMERGENCY_TYPES,
        "priority_labels": classifier.PRIORITY_LABELS,
        "policies": [{"id": p, "name": scheduler.POLICY_NAMES[p]} for p in scheduler.POLICIES],
        "locations": PRESET_LOCATIONS,
        "weights": config.RANK_WEIGHTS,
        "aging_interval_min": config.AGING_INTERVAL_MIN,
    })


# ---------------------------------------------------------------------------
# SOS + queue + dispatch
# ---------------------------------------------------------------------------
def _validate_sos(data):
    name = str(data.get("patient_name", "")).strip()
    if not name:
        raise ApiError("Patient name is required")
    if len(name) > 80:
        raise ApiError("Patient name is too long (max 80 characters)")
    try:
        lat, lon = float(data.get("latitude")), float(data.get("longitude"))
    except (TypeError, ValueError):
        raise ApiError("Latitude and longitude must be numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ApiError("Latitude/longitude out of range")
    phone = str(data.get("phone", "")).strip()[:20] or None
    description = str(data.get("description", "")).strip()[:255] or None
    return name, phone, lat, lon, description


def insert_request(name, phone, lat, lon, etype, severity, description, minutes_ago=0):
    c = classifier.classify(etype, severity)
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sos_requests (patient_name, phone, latitude, longitude, emergency_type, "
                "severity, description, priority, required_resource, required_specialist, "
                "est_handling_min, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW() - INTERVAL %s MINUTE)",
                (name, phone, lat, lon, c["emergency_type"], c["severity"], description,
                 c["priority"], c["required_resource"], c["required_specialist"],
                 c["est_handling_min"], int(minutes_ago)))
            rid = cur.lastrowid
            db.log_event(cur, "SOS_RECEIVED",
                         f"Request #{rid}: {c['emergency_type']} severity {c['severity']} -> "
                         f"P{c['priority']} ({c['priority_label']}), needs {c['required_resource']} "
                         f"+ {c['required_specialist']}", rid)
        conn.commit()
    finally:
        conn.close()
    return rid, c


@app.post("/api/sos")
def create_sos():
    data = body()
    name, phone, lat, lon, description = _validate_sos(data)
    rid, c = insert_request(name, phone, lat, lon, data.get("emergency_type"),
                            data.get("severity"), description)
    req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (rid,))
    ranked = ranking.rank_for_request(req)
    return jsonify({"ok": True, "request": req, "classification": c,
                    "preview": ranked[:3], "nearest": ranking.nearest_hospital(lat, lon)}), 201


@app.get("/api/queue")
def queue():
    policy = get_policy(request.args.get("policy"))
    ordered = scheduler.order_queue(pending_requests(), policy, now=db_now())
    return jsonify({"ok": True, "policy": policy, "policy_name": scheduler.POLICY_NAMES[policy],
                    "queue": ordered})


@app.get("/api/rank/<int:request_id>")
def rank(request_id):
    req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (request_id,))
    if not req:
        raise ApiError("Request not found", 404)
    return jsonify({"ok": True, "request": req, "ranked": ranking.rank_for_request(req),
                    "nearest": ranking.nearest_hospital(req["latitude"], req["longitude"])})


@app.post("/api/dispatch")
def dispatch():
    data = body()
    policy = get_policy(data.get("policy"))
    if data.get("request_id"):
        req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s AND is_lab = 0",
                           (int(data["request_id"]),))
        if not req:
            raise ApiError("Request not found", 404)
        position = None
    else:
        nxt = scheduler.next_request(pending_requests(), policy, now=db_now())
        if not nxt:
            return jsonify({"ok": True, "status": "empty", "message": "The queue is empty."})
        req, position = nxt, nxt["position"]

    ranked = ranking.rank_for_request(req)
    result = tm.reserve(req["request_id"], ranked=ranked)
    return jsonify({"ok": True, "policy": policy, "request": req, "ranked": ranked[:5],
                    "nearest": ranking.nearest_hospital(req["latitude"], req["longitude"]),
                    "picked_from_position": position, **result})


@app.get("/api/requests")
def list_requests():
    status = request.args.get("status")
    sql = ("SELECT r.*, a.allocation_id, h.name AS hospital_name FROM sos_requests r "
           "LEFT JOIN allocations a ON a.request_id = r.request_id "
           "LEFT JOIN hospitals h ON h.hospital_id = a.hospital_id WHERE r.is_lab = 0")
    params = []
    if status:
        sql += " AND r.status = %s"
        params.append(status.upper())
    sql += " ORDER BY r.request_id DESC LIMIT 100"
    return jsonify({"ok": True, "requests": db.query_all(sql, params)})


@app.get("/api/allocations")
def list_allocations():
    status = (request.args.get("status") or "ACTIVE").upper()
    rows = db.query_all(
        "SELECT a.*, r.patient_name, r.emergency_type, r.priority, r.required_resource, "
        "h.name AS hospital_name, d.name AS doctor_name, d.specialization "
        "FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id "
        "JOIN hospitals h ON h.hospital_id = a.hospital_id "
        "LEFT JOIN doctors d ON d.doctor_id = a.doctor_id "
        "WHERE a.status = %s AND h.is_lab = 0 ORDER BY a.allocation_id DESC LIMIT 100", (status,))
    return jsonify({"ok": True, "allocations": rows})


@app.post("/api/allocations/<int:allocation_id>/release")
def release(allocation_id):
    out = tm.release(allocation_id)
    if out["result"] == "not_found":
        raise ApiError("Allocation not found", 404)
    if out["result"] == "already_released":
        raise ApiError("Allocation was already released", 409)
    return jsonify({"ok": True, **out})


# ---------------------------------------------------------------------------
# Hospitals + admin
# ---------------------------------------------------------------------------
@app.get("/api/hospitals")
def hospitals():
    hosp = db.query_all("SELECT * FROM hospitals WHERE is_lab = 0 ORDER BY hospital_id")
    res = db.query_all("SELECT r.* FROM resources r JOIN hospitals h USING (hospital_id) "
                       "WHERE h.is_lab = 0 ORDER BY r.hospital_id, r.resource_type")
    docs = db.query_all("SELECT d.* FROM doctors d JOIN hospitals h USING (hospital_id) "
                        "WHERE h.is_lab = 0 ORDER BY d.hospital_id, d.specialization")
    by_id = {h["hospital_id"]: {**h, "resources": [], "doctors": []} for h in hosp}
    for r in res:
        by_id[r["hospital_id"]]["resources"].append(r)
    for d in docs:
        by_id[d["hospital_id"]]["doctors"].append(d)
    for h in by_id.values():
        total = sum(r["total_units"] for r in h["resources"])
        free = sum(r["available_units"] for r in h["resources"])
        h["occupancy_pct"] = round(100 * (total - free) / total, 1) if total else 0
    return jsonify({"ok": True, "hospitals": list(by_id.values())})


@app.post("/api/admin/resource")
def admin_resource():
    d = body()
    try:
        out = tm.update_resource(int(d["hospital_id"]), str(d["resource_type"]).upper(),
                                 int(d["total_units"]), int(d["available_units"]))
    except (KeyError, TypeError, ValueError) as e:
        if isinstance(e, tm.TxError):
            raise
        raise ApiError("hospital_id, resource_type, total_units and available_units are required "
                       "(whole numbers)")
    if out["result"] == "not_found":
        raise ApiError("That hospital does not have this resource type", 404)
    return jsonify({"ok": True, **out})


@app.post("/api/admin/doctor/<int:doctor_id>/shift")
def admin_shift(doctor_id):
    out = tm.set_doctor_shift(doctor_id, bool(body().get("on_shift")))
    if out["result"] == "not_found":
        raise ApiError("Doctor not found", 404)
    return jsonify({"ok": True, **out})


@app.post("/api/admin/reset")
def admin_reset():
    init_db.initialise()
    return jsonify({"ok": True, "message": "Simulated data reloaded"})


@app.get("/api/audit")
def audit():
    limit = max(1, min(int(request.args.get("limit", 60)), 500))
    rows = db.query_all("SELECT * FROM audit_log ORDER BY log_id DESC LIMIT %s", (limit,))
    return jsonify({"ok": True, "log": rows})


@app.post("/api/demo/seed")
def demo_seed():
    """Add six requests with staggered arrival times so the queue and aging are visible."""
    demo = [
        ("Ravi Negi", 30.2683, 77.9947, "GENERAL", 2, 45, "Fever and weakness"),
        ("Sita Devi", 30.3247, 78.0418, "MATERNITY", 3, 30, "Labour pains"),
        ("Mohan Lal", 30.2887, 78.0010, "TRAUMA", 4, 20, "Road accident, leg fracture"),
        ("Asha Rawat", 30.3484, 78.0618, "RESPIRATORY", 5, 12, "Severe breathlessness"),
        ("Kunal Thapa", 30.3340, 77.9590, "POISONING", 3, 6, "Food poisoning"),
        ("Geeta Bisht", 30.1760, 78.1210, "CARDIAC", 5, 1, "Chest pain, sweating"),
    ]
    ids = [insert_request(n, None, la, lo, t, s, desc, minutes_ago=m)[0]
           for n, la, lo, t, s, m, desc in demo]
    return jsonify({"ok": True, "created": ids})


# ---------------------------------------------------------------------------
# Labs
# ---------------------------------------------------------------------------
def _sample_items():
    with open(os.path.join(BASE_DIR, "sample_workload.json"), encoding="utf-8") as f:
        return json.load(f)["requests"]


@app.get("/api/labs/scheduler/workload")
def lab_workload():
    return jsonify({"ok": True, "workload": scheduler.workload_from_requests(_sample_items())})


@app.post("/api/labs/scheduler")
def lab_scheduler():
    d = body()
    items = d.get("requests") or _sample_items()
    if len(items) > 50:
        raise ApiError("At most 50 requests in a workload")
    aging = d.get("aging_interval", config.AGING_INTERVAL_MIN)
    workload = scheduler.workload_from_requests(items)
    return jsonify({"ok": True, "workload": workload,
                    "results": scheduler.compare(workload, aging_interval=float(aging))})


@app.post("/api/labs/race")
def lab_race():
    d = body()
    result = concurrency_lab.run_race(clients=int(d.get("clients", 6)),
                                      locked=bool(d.get("locked", True)),
                                      delay_ms=int(d.get("delay_ms", 50)))
    return jsonify({"ok": True, **result})


if __name__ == "__main__":
    print(f"EmergencySOS running on http://{config.FLASK_HOST}:{config.FLASK_PORT}")
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False, threaded=True)
