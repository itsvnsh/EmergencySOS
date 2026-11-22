"""
auth.py - login, roles and access control (Phase III).

Three roles (stored in the `users` table):
    PATIENT         send SOS requests and follow their own requests
    DISPATCHER      run the queue: dispatch, release, labs
    HOSPITAL_ADMIN  change beds, equipment, doctors and hospital details
                    hospital_id NULL  -> system admin, may edit every hospital
                    hospital_id = X   -> may edit hospital X only

Passwords are never stored as text: werkzeug's generate_password_hash() stores a
salted scrypt hash, and check_password_hash() compares in constant time.

The login lives in Flask's signed session cookie (only the user_id is stored).
Every protected API uses @require_role(...):
    not logged in       -> 401 Unauthorized
    wrong role          -> 403 Forbidden
"""
import functools

from flask import g, jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

import db

ROLES = ("PATIENT", "DISPATCHER", "HOSPITAL_ADMIN")
ROLE_NAMES = {"PATIENT": "Patient", "DISPATCHER": "Dispatcher", "HOSPITAL_ADMIN": "Hospital admin"}

# Demo accounts created by init_db.py (shown on the login page; change them for real use)
# (username, password, full name, role, hospital index or None)
DEMO_USERS = [
    ("admin", "admin123", "System Admin", "HOSPITAL_ADMIN", None),
    ("maxadmin", "admin123", "Max Hospital Admin", "HOSPITAL_ADMIN", 0),
    ("dispatcher", "dispatch123", "Control Room Dispatcher", "DISPATCHER", None),
    ("patient", "patient123", "Demo Patient", "PATIENT", None),
]


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.message, self.status = message, status


def hash_password(password):
    return generate_password_hash(password)


def validate_new_user(username, password, full_name, role):
    username = str(username or "").strip().lower()
    if not (3 <= len(username) <= 40) or not username.replace("_", "").replace(".", "").isalnum():
        raise ValueError("Username must be 3-40 letters, digits, '.' or '_'")
    if len(str(password or "")) < 6:
        raise ValueError("Password must be at least 6 characters")
    full_name = str(full_name or "").strip()
    if not full_name or len(full_name) > 80:
        raise ValueError("Full name is required (max 80 characters)")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}")
    return username, full_name


def create_user(cur, username, password, full_name, role, hospital_id=None, phone=None):
    """Insert a user with an EXISTING cursor (so callers control the transaction)."""
    username, full_name = validate_new_user(username, password, full_name, role)
    if role != "HOSPITAL_ADMIN":
        hospital_id = None
    cur.execute("INSERT INTO users (username, password_hash, full_name, phone, role, hospital_id) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (username, hash_password(password), full_name, phone, role, hospital_id))
    return cur.lastrowid


def public_user(u):
    """The safe part of a user row (never send the hash to the browser)."""
    if not u:
        return None
    return {"user_id": u["user_id"], "username": u["username"], "full_name": u["full_name"],
            "role": u["role"], "role_name": ROLE_NAMES[u["role"]], "phone": u.get("phone"),
            "hospital_id": u["hospital_id"], "hospital_name": u.get("hospital_name")}


def find_user(user_id):
    return db.query_one("SELECT u.*, h.name AS hospital_name FROM users u "
                        "LEFT JOIN hospitals h ON h.hospital_id = u.hospital_id "
                        "WHERE u.user_id = %s AND u.is_active = 1", (user_id,))


def authenticate(username, password):
    """Return the user row if the username/password pair is right, else None."""
    u = db.query_one("SELECT u.*, h.name AS hospital_name FROM users u "
                     "LEFT JOIN hospitals h ON h.hospital_id = u.hospital_id "
                     "WHERE u.username = %s AND u.is_active = 1",
                     (str(username or "").strip().lower(),))
    if not u or not check_password_hash(u["password_hash"], str(password or "")):
        return None
    db.execute("UPDATE users SET last_login_at = NOW() WHERE user_id = %s", (u["user_id"],))
    return u


def current_user():
    """The logged-in user for this request (cached on flask.g), or None."""
    if "user" not in g:
        uid = session.get("user_id")
        g.user = find_user(uid) if uid else None
        if uid and not g.user:            # account deleted or disabled -> log out
            session.clear()
    return g.user


def current_user_id():
    u = current_user()
    return u["user_id"] if u else None


def require_role(*roles):
    """Decorator: the caller must be logged in, and (if roles are given) have one of them."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                return jsonify({"ok": False, "error": "Please log in first", "login_required": True}), 401
            if roles and u["role"] not in roles:
                need = " or ".join(ROLE_NAMES[r] for r in roles)
                return jsonify({"ok": False, "error": f"Not allowed: this needs a {need} login"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco


def is_system_admin(u):
    return bool(u) and u["role"] == "HOSPITAL_ADMIN" and u["hospital_id"] is None


def check_hospital_access(hospital_id):
    """A hospital admin tied to one hospital may only change that hospital."""
    u = current_user()
    if u["role"] != "HOSPITAL_ADMIN":
        raise AuthError("Not allowed: this needs a Hospital admin login", 403)
    if u["hospital_id"] is not None and int(u["hospital_id"]) != int(hospital_id):
        raise AuthError(f"Not allowed: you can only manage {u['hospital_name']}", 403)


def require_system_admin():
    if not is_system_admin(current_user()):
        raise AuthError("Not allowed: only the system admin can do this", 403)
