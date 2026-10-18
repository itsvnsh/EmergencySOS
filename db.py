"""
db.py - small helper around PyMySQL.

Every call to get_connection() returns a NEW connection. This matters for the
concurrency lab: each simulated client needs its own connection (its own
database session), exactly like separate users of the real system.
"""
import pymysql
import pymysql.cursors

import config

# MySQL error codes we treat specially in the transaction manager
ER_LOCK_DEADLOCK = 1213
ER_LOCK_WAIT_TIMEOUT = 1205


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
    )
    with conn.cursor() as cur:
        cur.execute("SET SESSION innodb_lock_wait_timeout = %s",
                    (config.LOCK_WAIT_TIMEOUT_SEC,))
    return conn


def query_all(sql, params=None):
    """Run a read-only query on a short-lived connection and return all rows."""
    conn = get_connection(autocommit=True)
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
    conn = get_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.lastrowid
    finally:
        conn.close()


def log_event(cur, event_type, details, request_id=None, hospital_id=None):
    """Write an audit-log row using an EXISTING cursor (same transaction)."""
    cur.execute(
        "INSERT INTO audit_log (event_type, request_id, hospital_id, details) "
        "VALUES (%s, %s, %s, %s)",
        (event_type, request_id, hospital_id, details),
    )
