"""
config.py - central configuration for EmergencySOS.

Every value can be overridden with an environment variable, so each team
member can use their own MySQL username/password without editing code:

    Windows (PowerShell):  $env:DB_PASSWORD="mypassword"
    Linux / macOS:         export DB_PASSWORD=mypassword
"""
import json
import os

# Login saved by `python setup_mysql.py` (optional). Environment variables still win.
_LOGIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db_login.json")
_saved = {}
if os.path.exists(_LOGIN_FILE):
    with open(_LOGIN_FILE, encoding="utf-8") as _f:
        _saved = json.load(_f)


def _setting(name, default):
    return os.environ.get(name) or str(_saved.get(name, default))


DB_HOST = _setting("DB_HOST", "localhost")
DB_PORT = int(_setting("DB_PORT", "3306"))
DB_USER = _setting("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", str(_saved.get("DB_PASSWORD", "")))
DB_NAME = os.environ.get("DB_NAME", "emergencysos")

# Transaction settings (DBMS)
ISOLATION_LEVEL = "REPEATABLE READ"   # InnoDB default, stated explicitly
LOCK_WAIT_TIMEOUT_SEC = 5             # how long a transaction waits for a row lock
MAX_TX_RETRIES = 3                    # retries on deadlock / lock-wait timeout

# Scheduling settings (OS)
AGING_INTERVAL_MIN = 10               # every 10 min of waiting improves priority by 1

# Hospital ranking weights (must add up to 1.0)
RANK_WEIGHTS = {
    "distance": 0.40,
    "units": 0.25,
    "specialist": 0.20,
    "load": 0.15,
}

# Flask
FLASK_HOST = os.environ.get("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
