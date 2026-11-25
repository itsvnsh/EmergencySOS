"""
db.py - small helper around PyMySQL, with a connection pool.

Two ways to get a connection:

  get_connection()  opens a NEW connection (its own MySQL session) and close() really
                    closes it. The labs use this: every simulated client must be a
                    separate session, exactly like separate users of the real system.

  pooled()          borrows a connection from the POOL and close() gives it back.
                    The web app, the transaction manager and the query helpers use
                    this, so a request no longer pays for a TCP connect + MySQL login
                    every time (Tier 2).

The pool is a classic OS resource pool:
  - a counting semaphore with DB_POOL_SIZE permits = the connections that may be
    out at once; a thread that finds none free BLOCKS on the semaphore until one is
    returned (or DB_POOL_TIMEOUT_SEC passes)
  - idle connections are kept in a list (LIFO, so the warmest one is reused)
  - a connection is cleaned before it goes back (an open transaction is rolled back),
    and one that broke is thrown away instead of being reused
  - a thread that already holds a pooled connection and asks for another gets a
    fresh one instead of waiting: holding one connection while waiting for a second
    is hold-and-wait, and with every connection out that would deadlock the pool
"""
import os
import tempfile
import threading
import time

import pymysql
import pymysql.cursors

import config

# MySQL error codes we treat specially in the transaction manager
ER_LOCK_DEADLOCK = 1213
ER_LOCK_WAIT_TIMEOUT = 1205

SERVER_STATUS_IN_TRANS = 1        # bit in the server status: a transaction is open
PING_IF_IDLE_SEC = 30             # check a connection is still alive if it sat idle this long


_ssl_ca_file = None


def _ssl_options():
    """ssl= argument for PyMySQL (None = plain connection, as on a local MySQL)."""
    global _ssl_ca_file
    if not config.DB_SSL:
        return None
    ca = config.DB_SSL_CA.strip()
    if not ca:
        return {"verify_mode": False}          # encrypted, server certificate not checked
    if "BEGIN CERTIFICATE" in ca:              # PEM pasted into an environment variable
        if _ssl_ca_file is None:
            with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
                f.write(ca + "\n")
            _ssl_ca_file = f.name
        ca = _ssl_ca_file
    return {"ca": ca, "verify_mode": True}


def get_connection(database=True, autocommit=False):
    """Open a new connection. Rows come back as dictionaries."""
    conn = pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME if database else None,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
        ssl=_ssl_options(),
    )
    with conn.cursor() as cur:
        cur.execute("SET SESSION innodb_lock_wait_timeout = %s",
                    (config.LOCK_WAIT_TIMEOUT_SEC,))
    conn._sos_lock_timeout = config.LOCK_WAIT_TIMEOUT_SEC
    return conn


class PoolTimeout(Exception):
    pass


class ConnectionPool:
    def __init__(self, size, timeout):
        self.size = size
        self.timeout = timeout
        self._permits = threading.BoundedSemaphore(size)
        self._idle = []                       # (connection, time it was returned)
        self._lock = threading.Lock()
        self._held = {}                       # thread id -> pooled connections it holds
        self._db = config.DB_NAME
        self.stats = {"borrowed": 0, "created": 0, "reused": 0, "waited": 0, "wait_ms": 0.0,
                      "timeouts": 0, "discarded": 0, "nested": 0, "in_use": 0, "peak_in_use": 0}

    def _count(self, key, n=1):
        with self._lock:
            self.stats[key] += n

    def borrow(self, autocommit=False):
        me = threading.get_ident()
        if self._held.get(me):                # hold-and-wait guard (see module docstring)
            self._count("nested")
            return get_connection(autocommit=autocommit)
        t0 = time.perf_counter()
        if not self._permits.acquire(blocking=False):
            self._count("waited")
            if not self._permits.acquire(timeout=self.timeout):
                self._count("timeouts")
                raise PoolTimeout(f"No database connection free after {self.timeout} s "
                                  f"(pool size {self.size})")
            self._count("wait_ms", (time.perf_counter() - t0) * 1000)
        try:
            conn = self._take_idle()
            if conn is None:
                conn = get_connection(autocommit=autocommit)
                self._count("created")
            else:
                self._count("reused")
                if conn.get_autocommit() != autocommit:
                    conn.autocommit(autocommit)
                if conn._sos_lock_timeout != config.LOCK_WAIT_TIMEOUT_SEC:   # the load test changes it
                    with conn.cursor() as cur:
                        cur.execute("SET SESSION innodb_lock_wait_timeout = %s",
                                    (config.LOCK_WAIT_TIMEOUT_SEC,))
                    conn._sos_lock_timeout = config.LOCK_WAIT_TIMEOUT_SEC
        except Exception:
            self._permits.release()
            raise
        with self._lock:
            self.stats["borrowed"] += 1
            self.stats["in_use"] += 1
            self.stats["peak_in_use"] = max(self.stats["peak_in_use"], self.stats["in_use"])
            self._held[me] = self._held.get(me, 0) + 1
        return PooledConnection(self, conn, me)

    def _take_idle(self):
        while True:
            with self._lock:
                if not self._idle:
                    return None
                conn, since = self._idle.pop()            # LIFO: the most recently used
            if time.monotonic() - since < PING_IF_IDLE_SEC:
                return conn
            try:
                conn.ping(reconnect=False)
                return conn
            except Exception:
                self._discard(conn)

    def _discard(self, conn):
        self._count("discarded")
        try:
            conn.close()
        except Exception:
            pass

    def give_back(self, conn, owner):
        with self._lock:                      # the borrowing thread no longer holds it
            left = self._held.get(owner, 1) - 1
            if left > 0:
                self._held[owner] = left
            else:
                self._held.pop(owner, None)
        try:
            ok = bool(conn.open) and self._db == config.DB_NAME
            if ok and conn.server_status & SERVER_STATUS_IN_TRANS:
                conn.rollback()                           # never hand out a half-done transaction
        except Exception:
            ok = False
        if ok:
            with self._lock:
                ok = len(self._idle) < self.size
                if ok:
                    self._idle.append((conn, time.monotonic()))
        if not ok:
            self._discard(conn)
        with self._lock:
            self.stats["in_use"] -= 1
        self._permits.release()

    def close_idle(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for conn, _ in idle:
            try:
                conn.close()
            except Exception:
                pass

    def snapshot(self):
        with self._lock:
            s = dict(self.stats)
            s["idle"] = len(self._idle)
        s["size"] = self.size
        s["wait_ms"] = round(s["wait_ms"], 1)
        s["reuse_pct"] = round(100 * s["reused"] / max(1, s["reused"] + s["created"]), 1)
        return s


class PooledConnection:
    """Behaves like a PyMySQL connection; close() returns it to the pool."""

    def __init__(self, owner, conn, thread_id):
        self._owner, self._conn, self._thread = owner, conn, thread_id

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if self._conn is not None:
            conn, self._conn = self._conn, None
            self._owner.give_back(conn, self._thread)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_pool = None
_pool_guard = threading.Lock()
_pool_enabled = config.DB_POOL


def pool():
    """The process-wide pool (created on first use, recreated if DB_NAME changed)."""
    global _pool
    with _pool_guard:
        if _pool is None or _pool._db != config.DB_NAME:
            if _pool is not None:
                _pool.close_idle()
            _pool = ConnectionPool(config.DB_POOL_SIZE, config.DB_POOL_TIMEOUT_SEC)
        return _pool


def set_pool_enabled(on):
    """Switch pooling on/off at runtime (the load test compares both)."""
    global _pool_enabled
    _pool_enabled = bool(on)
    reset_pool()


def pool_enabled():
    return _pool_enabled


def reset_pool():
    """Close every idle connection and start a fresh pool (e.g. after the database is rebuilt)."""
    global _pool
    with _pool_guard:
        if _pool is not None:
            _pool.close_idle()
        _pool = None


def pool_stats():
    if not _pool_enabled:
        return {"enabled": False, "size": config.DB_POOL_SIZE}
    return {"enabled": True, **pool().snapshot()}


def pooled(autocommit=False):
    """A connection for one unit of work. Always close() it (or use `with`)."""
    if not _pool_enabled:
        return get_connection(autocommit=autocommit)
    return pool().borrow(autocommit=autocommit)


def query_all(sql, params=None):
    """Run a read-only query on a short-lived connection and return all rows."""
    conn = pooled(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


def query_one(sql, params=None):
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """Run a single write statement in its own (auto-committed) transaction."""
    conn = pooled(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.lastrowid
    finally:
        conn.close()


def _actor_id():
    """user_id of the logged-in web user, if this runs inside a Flask request."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            u = g.get("user")
            return u["user_id"] if u else None
    except ImportError:   # pragma: no cover
        pass
    return None


def log_event(cur, event_type, details, request_id=None, hospital_id=None, user_id=None):
    """Write an audit-log row using an EXISTING cursor (same transaction)."""
    cur.execute(
        "INSERT INTO audit_log (event_type, request_id, hospital_id, details, user_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (event_type, request_id, hospital_id, details, user_id or _actor_id()),
    )


def latest_change_id():
    """Id of the newest audit-log row. Every change writes one, so when this number
    moves, something changed - the live-update stream uses it as a cheap fingerprint."""
    row = query_one("SELECT COALESCE(MAX(log_id), 0) AS v FROM audit_log")
    return int(row["v"])
