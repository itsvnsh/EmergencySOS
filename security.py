"""
security.py - login rate limiting and CSRF protection (Tier 3).

Rate limiting (sliding window)
------------------------------
Every failed login is remembered with its time, per (IP, username) and per IP.
    5 failures for one username from one IP in 5 minutes  -> that pair is locked
    20 failures from one IP in 5 minutes (any usernames)   -> that IP is locked
A locked login is refused with 429 Too Many Requests and a Retry-After header,
WITHOUT checking the password, so guessing gets no answer at all. The lock ends
by itself when the oldest failure leaves the window; a correct login clears the
pair's failures. Anonymous SOS requests are limited too (10 a minute per IP), so
one script cannot flood the dispatch queue; logged-in staff are not limited.

The counters live in memory, shared by all request threads, so every update is a
critical section guarded by one lock.

CSRF (cross-site request forgery)
---------------------------------
The login is a cookie, and a browser sends cookies with requests that OTHER sites
trigger. So every state-changing API call (POST/PUT/PATCH/DELETE under /api/) must:
  1. carry the session's secret token in the X-CSRF-Token header. The token is
     put in the page (<meta name="csrf-token">) and is also returned by
     GET /api/csrf; another site can neither read it nor set custom headers on
     a cross-site form post.
  2. not come from another origin: if the browser sends Origin (or
     Sec-Fetch-Site: cross-site), it must be this site.
The session cookie is also HttpOnly and SameSite=Lax (app.py), a third layer.
"""
import collections
import hmac
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import jsonify, request, session

RATE_LIMIT = os.environ.get("RATE_LIMIT", "1") == "1"
CSRF_CHECK = os.environ.get("CSRF_CHECK", "1") == "1"

LOGIN_WINDOW_SEC = 300
LOGIN_MAX_FAILS_USER = 5        # per (IP, username)
LOGIN_MAX_FAILS_IP = 20         # per IP, all usernames together
SOS_WINDOW_SEC = 60
SOS_MAX_PER_IP = int(os.environ.get("SOS_MAX_PER_MIN", "10"))

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "X-CSRF-Token"


class SlidingWindow:
    """Remembers event times per key and counts those inside the last `window` seconds."""

    def __init__(self, window, limit, clock=time.monotonic):
        self.window, self.limit, self.clock = window, limit, clock
        self._events = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def _trim(self, q, now):
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, key):
        """Seconds until `key` may try again (0 = allowed now)."""
        with self._lock:
            q = self._events.get(key)
            if not q:
                return 0
            now = self.clock()
            self._trim(q, now)
            if len(q) < self.limit:
                return 0
            return max(1, int(q[0] + self.window - now + 0.999))

    def hit(self, key):
        """Record one event; returns how many are now inside the window."""
        with self._lock:
            now = self.clock()
            q = self._events[key]
            self._trim(q, now)
            q.append(now)
            if len(self._events) > 10000:            # forget idle keys so memory stays small
                for k in [k for k, v in self._events.items() if not v or v[-1] <= now - self.window]:
                    del self._events[k]
            return len(q)

    def clear(self, key):
        with self._lock:
            self._events.pop(key, None)

    def reset(self):
        with self._lock:
            self._events.clear()


login_user_fails = SlidingWindow(LOGIN_WINDOW_SEC, LOGIN_MAX_FAILS_USER)
login_ip_fails = SlidingWindow(LOGIN_WINDOW_SEC, LOGIN_MAX_FAILS_IP)
sos_per_ip = SlidingWindow(SOS_WINDOW_SEC, SOS_MAX_PER_IP)


def reset_limits():
    """Forget every counter (tests, or an admin after a false alarm)."""
    for w in (login_user_fails, login_ip_fails, sos_per_ip):
        w.reset()


def client_ip():
    return request.remote_addr or "?"


def _user_key(username):
    return f"{client_ip()}|{str(username or '').strip().lower()}"


def login_retry_after(username):
    """0 if this login may be checked now, else seconds to wait."""
    if not RATE_LIMIT:
        return 0
    return max(login_user_fails.retry_after(_user_key(username)), login_ip_fails.retry_after(client_ip()))


def login_failed(username):
    """Record a failure. Returns (failures for this user from this IP, now locked?)."""
    if not RATE_LIMIT:
        return 0, False
    n = login_user_fails.hit(_user_key(username))
    login_ip_fails.hit(client_ip())
    return n, login_retry_after(username) > 0


def login_succeeded(username):
    login_user_fails.clear(_user_key(username))


def sos_retry_after():
    """Count one anonymous SOS from this IP; seconds to wait if over the limit (the SOS is refused)."""
    if not RATE_LIMIT:
        return 0
    wait = sos_per_ip.retry_after(client_ip())
    if wait:
        return wait
    sos_per_ip.hit(client_ip())
    return 0


def too_many(message, retry_after):
    resp = jsonify({"ok": False, "error": message, "retry_after": retry_after})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry_after)
    return resp


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
def csrf_token():
    """This session's token (created on first use; a new login gets a new one)."""
    tok = session.get("csrf")
    if not tok:
        tok = session["csrf"] = secrets.token_urlsafe(32)
    return tok


def _same_origin(origin):
    try:
        u = urlsplit(origin)
    except ValueError:
        return False
    return bool(u.scheme in ("http", "https") and u.netloc and u.netloc.lower() == request.host.lower())


def csrf_problem():
    """None if this request passes the CSRF checks, else the reason it is refused."""
    if not CSRF_CHECK or request.method not in UNSAFE_METHODS or not request.path.startswith("/api/"):
        return None
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return "Cross-site request blocked"
    origin = request.headers.get("Origin")
    if origin and origin != "null" and not _same_origin(origin):
        return "Cross-site request blocked"
    if origin == "null":
        return "Cross-site request blocked"
    sent = request.headers.get(CSRF_HEADER, "")
    expected = session.get("csrf", "")
    if not sent or not expected or not hmac.compare_digest(sent, expected):
        return "Security token missing or expired - reload the page and try again"
    return None
