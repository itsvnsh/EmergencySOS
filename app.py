"""
app.py - Flask REST API + web console for EmergencySOS.

Run:   python app.py      then open http://127.0.0.1:5000
       (console: /  -> login page for staff,  mobile SOS portal: /sos)

Roles: P = patient, D = dispatcher, A = hospital admin, * = anyone (no login)

API (all JSON):
  POST /api/login  /api/logout,  GET /api/me   *  session login (signed cookie)
  GET  /api/meta                          *  emergency types, policies, preset locations
  POST /api/sos                           *  create an SOS request (classified on arrival)
  GET  /api/sos/<id>/status?token=        *  follow one request (token, owner or staff)
  GET  /api/my/requests                   P  a patient's own requests
  GET  /api/stream                        *  live updates (Server-Sent Events)
  GET  /api/map                           D,A hospitals, waiting patients, routes for the map
  GET  /api/routing/status                D,A road routing on, or offline estimate
  GET  /api/queue                         pending queue in Automatic order (?policy=FCFS etc. to compare)
  GET  /api/rank/<request_id>             ranked hospitals for a request (preview)
  POST /api/dispatch                      serve the next request (Automatic mode by default)
  GET  /api/requests                      recent requests
  GET  /api/allocations?status=ACTIVE     allocations
  POST /api/allocations/<id>/release      finish a case, return the unit
  GET  /api/hospitals                     capacity dashboard data
  POST /api/admin/resource                change unit counts
  POST /api/admin/doctor/<id>/shift       toggle a doctor's shift
  POST /api/admin/reset                   reload the simulated data
  GET  /api/audit                         audit log
  POST /api/demo/seed                     add demo requests with staggered arrival times
  GET  /api/labs/scheduler/workload       workload for the lab (?source=real -> your patients, sample -> 12 requests)
  POST /api/labs/scheduler                compare scheduling policies {source: "real"|"sample"}
  POST /api/labs/race                     run the concurrency race
  Admin (A): POST /api/admin/hospitals, PUT/DELETE /api/admin/hospitals/<id>,
             POST /api/admin/hospitals/<id>/doctors, PUT/DELETE /api/admin/doctors/<id>,
             GET/POST /api/admin/users, POST /api/admin/users/<id>/active
  Patients (D,A): PUT /api/requests/<id>   (D): POST /api/requests/<id>/cancel
Queue, dispatch, release, labs and demo data need a dispatcher (D);
admin endpoints need a hospital admin (A), limited to their own hospital.
"""
import datetime as dt
import decimal
import json
import os
import secrets
import threading
import time

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from flask.json.provider import DefaultJSONProvider

import admin_ops
import auth
import classifier
import concurrency_lab
import config
import db
import init_db
import ranking
import routing
import scheduler
import transaction_manager as tm
from auth import require_role

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


def _secret_key():
    """Signs the login cookie. Uses SECRET_KEY if set, else a random key saved in .secret_key."""
    if config.SECRET_KEY:
        return config.SECRET_KEY
    path = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.json = JSONProvider(app)
app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12))

STAFF = ("DISPATCHER", "HOSPITAL_ADMIN")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({"ok": False, "error": e.message}), e.status


@app.errorhandler(auth.AuthError)
def handle_auth_error(e):
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
    policy = (value or "AUTO").upper()
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
    u = auth.current_user()
    if not u:
        return redirect(url_for("login_page"))
    if u["role"] == "PATIENT":
        return redirect(url_for("sos_portal"))
    return render_template("index.html")


@app.route("/login")
def login_page():
    demo = [{"username": u, "password": p, "role": auth.ROLE_NAMES[r], "name": n}
            for u, p, n, r, _ in auth.DEMO_USERS]
    return render_template("login.html", demo=demo)


@app.route("/sos")
def sos_portal():
    return render_template("sos.html")


@app.post("/api/login")
def login():
    d = body()
    u = auth.authenticate(d.get("username"), d.get("password"))
    if not u:
        raise ApiError("Wrong username or password", 401)
    session.clear()
    session["user_id"] = u["user_id"]
    session.permanent = True
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            db.log_event(cur, "LOGIN", f"{u['username']} logged in ({u['role']})", user_id=u["user_id"])
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "user": auth.public_user(u),
                    "home": "/sos" if u["role"] == "PATIENT" else "/"})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    u = auth.current_user()
    return jsonify({"ok": True, "user": auth.public_user(u),
                    "system_admin": auth.is_system_admin(u)})


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
        "specializations": admin_ops.SPECIALIZATIONS,
        "resource_types": admin_ops.RESOURCE_TYPES,
        "routing": routing.status(),
        "auto_complete": {"on": config.AUTO_COMPLETE, "time_scale": config.CASE_TIME_SCALE},
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


def insert_request(name, phone, lat, lon, etype, severity, description, minutes_ago=0, user_id=None):
    c = classifier.classify(etype, severity)
    token = secrets.token_hex(8)              # 16 chars: lets the sender follow this request
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sos_requests (patient_name, phone, latitude, longitude, emergency_type, "
                "severity, description, priority, required_resource, required_specialist, "
                "est_handling_min, user_id, track_token, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW() - INTERVAL %s MINUTE)",
                (name, phone, lat, lon, c["emergency_type"], c["severity"], description,
                 c["priority"], c["required_resource"], c["required_specialist"],
                 c["est_handling_min"], user_id, token, int(minutes_ago)))
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
    """Open to everyone: in an emergency nobody should be stopped by a login screen.
    A logged-in patient's request is linked to their account; anyone else gets a
    tracking token to follow the request on the mobile portal."""
    data = body()
    name, phone, lat, lon, description = _validate_sos(data)
    u = auth.current_user()
    rid, c = insert_request(name, phone, lat, lon, data.get("emergency_type"),
                            data.get("severity"), description,
                            user_id=u["user_id"] if u and u["role"] == "PATIENT" else None)
    req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (rid,))
    ranked = ranking.rank_for_request(req)
    return jsonify({"ok": True, "request": _public_request(req), "classification": c,
                    "track_token": req["track_token"],
                    "preview": ranked[:3], "nearest": ranking.nearest_hospital(lat, lon)}), 201


def _public_request(r):
    """Request row without the tracking token (the token is only given to the sender)."""
    return {k: v for k, v in r.items() if k != "track_token"}


def _queue_position(req):
    pending = pending_requests()
    ordered = scheduler.order_queue(pending, "AUTO", now=db_now())
    for q in ordered:
        if q["request_id"] == req["request_id"]:
            return q["position"], len(ordered), q.get("auto")
    return None, len(ordered), None


@app.get("/api/sos/<int:request_id>/status")
def sos_status(request_id):
    """Live status of one request for the patient portal.
    Allowed for: the tracking token, the patient who sent it, or staff."""
    req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s AND is_lab = 0", (request_id,))
    u = auth.current_user()
    allowed = req and (
        (u and u["role"] in STAFF)
        or (u and req["user_id"] == u["user_id"])
        or secrets.compare_digest(str(request.args.get("token", "")), str(req["track_token"] or "-")))
    if not allowed:
        raise ApiError("Request not found", 404)       # same answer: do not reveal it exists
    out = {"ok": True, "request": _public_request(req),
           "priority_label": classifier.PRIORITY_LABELS[req["priority"]]}
    if req["status"] == "PENDING":
        pos, total, auto = _queue_position(req)
        out.update(queue_position=pos, queue_length=total, queue=auto and auto["queue"])
    alloc = db.query_one(
        "SELECT a.*, h.name AS hospital_name, h.phone AS hospital_phone, h.area AS hospital_area, "
        "h.latitude AS h_lat, h.longitude AS h_lon, d.name AS doctor_name, d.specialization "
        "FROM allocations a JOIN hospitals h ON h.hospital_id = a.hospital_id "
        "LEFT JOIN doctors d ON d.doctor_id = a.doctor_id "
        "WHERE a.request_id = %s ORDER BY a.allocation_id DESC LIMIT 1", (request_id,))
    if alloc:
        out["allocation"] = alloc
        out["route"] = routing.route(req["latitude"], req["longitude"], alloc["h_lat"], alloc["h_lon"])
    return jsonify(out)


@app.get("/api/my/requests")
@require_role("PATIENT")
def my_requests():
    rows = db.query_all(
        "SELECT r.request_id, r.patient_name, r.emergency_type, r.severity, r.priority, r.status, "
        "r.created_at, h.name AS hospital_name FROM sos_requests r "
        "LEFT JOIN allocations a ON a.request_id = r.request_id "
        "LEFT JOIN hospitals h ON h.hospital_id = a.hospital_id "
        "WHERE r.user_id = %s ORDER BY r.request_id DESC LIMIT 20", (auth.current_user_id(),))
    return jsonify({"ok": True, "requests": rows})


@app.get("/api/queue")
@require_role(*STAFF)
def queue():
    policy = get_policy(request.args.get("policy"))
    ordered = scheduler.order_queue(pending_requests(), policy, now=db_now())
    return jsonify({"ok": True, "policy": policy, "policy_name": scheduler.POLICY_NAMES[policy],
                    "queue": ordered})


@app.get("/api/rank/<int:request_id>")
@require_role(*STAFF)
def rank(request_id):
    req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (request_id,))
    if not req:
        raise ApiError("Request not found", 404)
    return jsonify({"ok": True, "request": req, "ranked": ranking.rank_for_request(req),
                    "nearest": ranking.nearest_hospital(req["latitude"], req["longitude"])})


@app.post("/api/dispatch")
@require_role("DISPATCHER")
def dispatch():
    data = body()
    policy = get_policy(data.get("policy"))
    if data.get("request_id"):
        req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s AND is_lab = 0",
                           (int(data["request_id"]),))
        if not req:
            raise ApiError("Request not found", 404)
        position = None
        decision = None
    else:
        nxt = scheduler.next_request(pending_requests(), policy, now=db_now())
        if not nxt:
            return jsonify({"ok": True, "status": "empty", "message": "The queue is empty."})
        req, position = nxt, nxt["position"]
    decision = req.get("auto") if policy == "AUTO" else None

    ranked = ranking.rank_for_request(req)
    result = tm.reserve(req["request_id"], ranked=ranked)
    return jsonify({"ok": True, "policy": policy, "request": req, "ranked": ranked[:5],
                    "nearest": ranking.nearest_hospital(req["latitude"], req["longitude"]),
                    "picked_from_position": position, "decision": decision, **result})


@app.get("/api/requests")
@require_role(*STAFF)
def list_requests():
    status = request.args.get("status")
    sql = ("SELECT r.request_id, r.patient_name, r.phone, r.latitude, r.longitude, r.emergency_type, "
           "r.severity, r.description, r.priority, r.required_resource, r.required_specialist, "
           "r.est_handling_min, r.status, r.user_id, r.created_at, r.updated_at, "
           "a.allocation_id, h.name AS hospital_name FROM sos_requests r "
           "LEFT JOIN allocations a ON a.request_id = r.request_id "
           "LEFT JOIN hospitals h ON h.hospital_id = a.hospital_id WHERE r.is_lab = 0")
    params = []
    if status:
        sql += " AND r.status = %s"
        params.append(status.upper())
    sql += " ORDER BY r.request_id DESC LIMIT 100"
    return jsonify({"ok": True, "requests": db.query_all(sql, params)})


@app.get("/api/allocations")
@require_role(*STAFF)
def list_allocations():
    status = (request.args.get("status") or "ACTIVE").upper()
    rows = db.query_all(
        "SELECT a.*, r.patient_name, r.emergency_type, r.priority, r.required_resource, "
        "h.name AS hospital_name, h.phone AS hospital_phone, d.name AS doctor_name, d.specialization "
        "FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id "
        "JOIN hospitals h ON h.hospital_id = a.hospital_id "
        "LEFT JOIN doctors d ON d.doctor_id = a.doctor_id "
        "WHERE a.status = %s AND h.is_lab = 0 ORDER BY a.allocation_id DESC LIMIT 100", (status,))
    # server clock lets the browser show an accurate "auto-completes in m:ss" countdown
    return jsonify({"ok": True, "allocations": rows, "server_time": db_now()})


@app.post("/api/allocations/<int:allocation_id>/release")
@require_role("DISPATCHER")
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
@require_role(*STAFF)
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
        care = [r for r in h["resources"] if r["resource_type"] != "AMBULANCE"]   # beds/OTs, not vehicles
        total = sum(r["total_units"] for r in care)
        free = sum(r["available_units"] for r in care)
        h["occupancy_pct"] = round(100 * (total - free) / total, 1) if total else 0
    return jsonify({"ok": True, "hospitals": list(by_id.values())})


@app.post("/api/admin/resource")
@require_role("HOSPITAL_ADMIN")
def admin_resource():
    d = body()
    try:
        auth.check_hospital_access(int(d["hospital_id"]))
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
@require_role("HOSPITAL_ADMIN")
def admin_shift(doctor_id):
    hid = admin_ops.doctor_hospital(doctor_id)
    if hid is None:
        raise ApiError("Doctor not found", 404)
    auth.check_hospital_access(hid)
    out = tm.set_doctor_shift(doctor_id, bool(body().get("on_shift")))
    if out["result"] == "not_found":
        raise ApiError("Doctor not found", 404)
    return jsonify({"ok": True, **out})


@app.post("/api/admin/reset")
@require_role("HOSPITAL_ADMIN")
def admin_reset():
    auth.require_system_admin()
    init_db.initialise()
    session.clear()                      # user ids were recreated -> log in again
    return jsonify({"ok": True, "message": "Simulated data reloaded. Please log in again.",
                    "relogin": True})


def _result(out, not_found="Not found"):
    if out.get("result") == "not_found":
        raise ApiError(not_found, 404)
    return jsonify({"ok": True, **out})


@app.post("/api/admin/hospitals")
@require_role("HOSPITAL_ADMIN")
def admin_add_hospital():
    auth.require_system_admin()
    return _result(admin_ops.add_hospital(body())), 201


@app.put("/api/admin/hospitals/<int:hospital_id>")
@require_role("HOSPITAL_ADMIN")
def admin_update_hospital(hospital_id):
    auth.check_hospital_access(hospital_id)
    return _result(admin_ops.update_hospital(hospital_id, body()), "Hospital not found")


@app.delete("/api/admin/hospitals/<int:hospital_id>")
@require_role("HOSPITAL_ADMIN")
def admin_delete_hospital(hospital_id):
    auth.require_system_admin()
    return _result(admin_ops.delete_hospital(hospital_id), "Hospital not found")


@app.post("/api/admin/hospitals/<int:hospital_id>/doctors")
@require_role("HOSPITAL_ADMIN")
def admin_add_doctor(hospital_id):
    auth.check_hospital_access(hospital_id)
    return _result(admin_ops.add_doctor(hospital_id, body()), "Hospital not found"), 201


def _doctor_access(doctor_id):
    hid = admin_ops.doctor_hospital(doctor_id)
    if hid is None:
        raise ApiError("Doctor not found", 404)
    auth.check_hospital_access(hid)


@app.put("/api/admin/doctors/<int:doctor_id>")
@require_role("HOSPITAL_ADMIN")
def admin_update_doctor(doctor_id):
    _doctor_access(doctor_id)
    return _result(admin_ops.update_doctor(doctor_id, body()), "Doctor not found")


@app.delete("/api/admin/doctors/<int:doctor_id>")
@require_role("HOSPITAL_ADMIN")
def admin_delete_doctor(doctor_id):
    _doctor_access(doctor_id)
    return _result(admin_ops.delete_doctor(doctor_id), "Doctor not found")


@app.get("/api/admin/users")
@require_role("HOSPITAL_ADMIN")
def admin_users():
    auth.require_system_admin()
    return jsonify({"ok": True, "users": admin_ops.list_users()})


@app.post("/api/admin/users")
@require_role("HOSPITAL_ADMIN")
def admin_add_user():
    auth.require_system_admin()
    return _result(admin_ops.create_user(body())), 201


@app.post("/api/admin/users/<int:user_id>/active")
@require_role("HOSPITAL_ADMIN")
def admin_user_active(user_id):
    auth.require_system_admin()
    return _result(admin_ops.set_user_active(user_id, bool(body().get("active")), auth.current_user_id()),
                   "User not found")


@app.put("/api/requests/<int:request_id>")
@require_role(*STAFF)
def update_request(request_id):
    return _result(admin_ops.update_request(request_id, body()), "Request not found")


@app.post("/api/requests/<int:request_id>/cancel")
@require_role("DISPATCHER")
def cancel_request(request_id):
    return _result(admin_ops.cancel_request(request_id), "Request not found")


# ---------------------------------------------------------------------------
# Map + routing
# ---------------------------------------------------------------------------
@app.get("/api/routing/status")
@require_role(*STAFF)
def routing_status():
    return jsonify({"ok": True, **routing.status()})


@app.get("/api/map")
@require_role(*STAFF)
def map_data():
    """Everything the map view draws: hospitals, waiting patients, and active cases with their road route."""
    hosp = db.query_all(
        "SELECT h.hospital_id, h.name, h.area, h.phone, h.latitude, h.longitude, "
        "SUM(r.total_units) AS total_units, SUM(r.available_units) AS free_units, "
        "SUM(CASE WHEN r.resource_type = 'ICU_BED' THEN r.available_units ELSE 0 END) AS free_icu, "
        "SUM(CASE WHEN r.resource_type = 'VENTILATOR' THEN r.available_units ELSE 0 END) AS free_vent, "
        "SUM(CASE WHEN r.resource_type = 'AMBULANCE' THEN r.available_units ELSE 0 END) AS free_amb "
        "FROM hospitals h JOIN resources r ON r.hospital_id = h.hospital_id "
        "WHERE h.is_lab = 0 GROUP BY h.hospital_id ORDER BY h.hospital_id")
    waiting = db.query_all(
        "SELECT request_id, patient_name, emergency_type, priority, latitude, longitude, created_at "
        "FROM sos_requests WHERE status = 'PENDING' AND is_lab = 0 ORDER BY priority, created_at LIMIT 100")
    active = db.query_all(
        "SELECT a.allocation_id, a.distance_km, a.travel_min, a.distance_source, a.expected_end_at, "
        "r.request_id, r.patient_name, r.emergency_type, r.priority, r.latitude, r.longitude, "
        "h.hospital_id, h.name AS hospital_name, h.latitude AS h_lat, h.longitude AS h_lon "
        "FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id "
        "JOIN hospitals h ON h.hospital_id = a.hospital_id "
        "WHERE a.status = 'ACTIVE' AND h.is_lab = 0 ORDER BY a.allocation_id DESC LIMIT 30")
    for a in active:
        a["route"] = routing.route(a["latitude"], a["longitude"], a["h_lat"], a["h_lon"])
    return jsonify({"ok": True, "hospitals": hosp, "waiting": waiting, "active": active,
                    "routing": routing.status()})


# ---------------------------------------------------------------------------
# Live updates (Server-Sent Events)
# ---------------------------------------------------------------------------
class ChangeWatcher:
    """
    ONE background thread checks the newest audit-log id once a second (every change in
    the system writes an audit row, even changes made from the terminal), and wakes up
    all connected browsers when it moves. So 30 open consoles still cost one tiny query/s.
    """
    def __init__(self):
        self.version = None
        self.cond = threading.Condition()
        self.started = False

    def start(self):
        with self.cond:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self._run, name="change-watcher", daemon=True).start()

    def _run(self):
        while True:
            try:
                v = db.latest_change_id()
            except Exception:          # database briefly unavailable: try again next second
                v = None
            if v is not None and v != self.version:
                with self.cond:
                    self.version = v
                    self.cond.notify_all()
            time.sleep(1)

    def wait_for_change(self, seen, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.version is not None and self.version != seen, timeout)
            return self.version


watcher = ChangeWatcher()


@app.get("/api/stream")
def stream():
    watcher.start()

    def events():
        seen = None
        yield "retry: 3000\n\n"                       # browser reconnects after 3 s if dropped
        while True:
            v = watcher.wait_for_change(seen, timeout=15)
            if v is not None and v != seen:
                seen = v
                yield f"event: change\ndata: {json.dumps({'version': v})}\n\n"
            else:
                yield ": keep-alive\n\n"              # comment line, ignored by the browser

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Automatic case completion worker
# ---------------------------------------------------------------------------
_worker_started = threading.Event()


def start_auto_complete_worker():
    """Background thread: every few seconds, bring back ambulances whose round trip is over
    and finish cases whose time is over."""
    if not config.AUTO_COMPLETE or _worker_started.is_set():
        return
    _worker_started.set()

    def run():
        while True:
            try:
                back = tm.return_due_ambulances()
                if back:
                    app.logger.info("ambulances back for allocations %s", back)
                done = tm.complete_due_cases()
                if done:
                    app.logger.info("auto-completed allocations %s", done)
            except Exception as e:      # keep the worker alive (e.g. MySQL restarted)
                app.logger.warning("auto-complete check failed: %s", e)
            time.sleep(config.AUTO_COMPLETE_EVERY_SEC)

    threading.Thread(target=run, name="auto-complete", daemon=True).start()


@app.get("/api/audit")
@require_role(*STAFF)
def audit():
    limit = max(1, min(int(request.args.get("limit", 60)), 500))
    rows = db.query_all("SELECT l.*, u.username FROM audit_log l LEFT JOIN users u ON u.user_id = l.user_id "
                        "ORDER BY l.log_id DESC LIMIT %s", (limit,))
    return jsonify({"ok": True, "log": rows})


@app.post("/api/demo/seed")
@require_role("DISPATCHER")
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


def _real_workload():
    """Patients really in the system: those holding an active allocation, plus those still waiting."""
    rows = db.query_all(
        "SELECT r.*, CASE WHEN r.status = 'PENDING' THEN 'WAITING' ELSE 'ACTIVE ALLOCATION' END AS live_status "
        "FROM sos_requests r WHERE r.is_lab = 0 AND (r.status = 'PENDING' OR EXISTS "
        "(SELECT 1 FROM allocations a WHERE a.request_id = r.request_id AND a.status = 'ACTIVE')) "
        "ORDER BY r.created_at, r.request_id LIMIT 50")
    return scheduler.workload_from_db_rows(rows)


def _lab_workload(source):
    if (source or "real") == "sample":
        return scheduler.workload_from_requests(_sample_items())
    return _real_workload()


@app.get("/api/labs/scheduler/workload")
@require_role(*STAFF)
def lab_workload():
    source = request.args.get("source", "real")
    return jsonify({"ok": True, "source": source, "workload": _lab_workload(source)})


@app.post("/api/labs/scheduler")
@require_role(*STAFF)
def lab_scheduler():
    d = body()
    aging = d.get("aging_interval", config.AGING_INTERVAL_MIN)
    if d.get("requests"):                                   # custom workload (used by tests / API)
        if len(d["requests"]) > 50:
            raise ApiError("At most 50 requests in a workload")
        workload = scheduler.workload_from_requests(d["requests"])
    else:
        workload = _lab_workload(d.get("source", "real"))
    if not workload:
        raise ApiError("There are no patients in the system yet. Send a few SOS requests and dispatch them, "
                       "or choose 'Sample workload'.")
    return jsonify({"ok": True, "workload": workload,
                    "results": scheduler.compare(workload, aging_interval=float(aging))})


@app.post("/api/labs/race")
@require_role(*STAFF)
def lab_race():
    d = body()
    result = concurrency_lab.run_race(clients=int(d.get("clients", 6)),
                                      locked=bool(d.get("locked", True)),
                                      delay_ms=int(d.get("delay_ms", 50)))
    return jsonify({"ok": True, **result})


if __name__ == "__main__":
    print(f"EmergencySOS running on http://{config.FLASK_HOST}:{config.FLASK_PORT}")
    print(f"  staff console : http://{config.FLASK_HOST}:{config.FLASK_PORT}/   (log in)")
    print(f"  patient portal: http://{config.FLASK_HOST}:{config.FLASK_PORT}/sos")
    print(f"  road routing  : {routing.status()['mode']}   "
          f"auto-completion: {'on' if config.AUTO_COMPLETE else 'off'} (time scale {config.CASE_TIME_SCALE})")
    start_auto_complete_worker()
    watcher.start()
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False, threaded=True)
