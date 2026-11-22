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

# Road-distance routing (Phase III)
# "osrm"  = road distance + travel time from an OSRM routing server (needs internet)
# "off"   = no network calls; road distance is estimated as straight line x ROAD_FACTOR
ROUTING_MODE = os.environ.get("ROUTING_MODE", "osrm").lower()
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org")
ROUTING_TIMEOUT_SEC = 4               # give up on the routing server after this long
ROAD_FACTOR = 1.35                    # typical road/straight-line ratio used by the fallback
AVG_SPEED_KMPH = 30                   # city ambulance speed used by the fallback

# Automatic case completion (Phase III)
# A case is finished after est_handling_min x CASE_TIME_SCALE minutes; the worker then
# releases the unit with the same locked transaction as a manual release.
# For a quick demo use e.g. CASE_TIME_SCALE=0.1 (a 13-minute case finishes in ~1.3 min).
AUTO_COMPLETE = os.environ.get("AUTO_COMPLETE", "1") == "1"
CASE_TIME_SCALE = float(os.environ.get("CASE_TIME_SCALE", "1"))
AUTO_COMPLETE_EVERY_SEC = 10          # how often the worker checks

# Flask
FLASK_HOST = os.environ.get("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
# Signs the login cookie. Set your own in production; a random one is saved on first run.
SECRET_KEY = os.environ.get("SECRET_KEY") or str(_saved.get("SECRET_KEY", ""))
