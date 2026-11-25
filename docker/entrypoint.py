"""
docker/entrypoint.py - start-up for the app container (Tier 3).

1. wait until MySQL accepts connections (the database container may still be starting)
2. on the very first start (no tables yet) create the tables, views, triggers,
   procedures and demo data with init_db.initialise(); later starts keep the data
3. start the web app exactly like `python app.py`
"""
import os
import runpy
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import config   # noqa: E402
import db       # noqa: E402

WAIT_SEC = int(os.environ.get("DB_WAIT_SEC", "90"))


def wait_for_mysql():
    deadline = time.monotonic() + WAIT_SEC
    while True:
        try:
            db.get_connection(database=False, autocommit=True).close()
            return
        except Exception as e:          # noqa: BLE001 - any connection error: keep waiting
            if time.monotonic() > deadline:
                print(f"MySQL at {config.DB_HOST}:{config.DB_PORT} not reachable after {WAIT_SEC} s: {e}")
                sys.exit(1)
            print(f"waiting for MySQL at {config.DB_HOST}:{config.DB_PORT} ...")
            time.sleep(2)


def has_tables():
    conn = db.get_connection(database=False, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables "
                        "WHERE table_schema = %s AND table_name = 'hospitals'", (config.DB_NAME,))
            return cur.fetchone()["n"] > 0
    finally:
        conn.close()


# Earlier demo data placed the hospitals at approximate points (Graphic Era Hospital
# ~14 km off). Move a hospital to its OpenStreetMap location only while it still has
# the old approximate point, so changes made in the admin console are kept.
OLD_LOCATIONS = {
    "Max Super Speciality Hospital": (30.3735, 78.0758),
    "Shri Mahant Indiresh Hospital": (30.3005, 78.0135),
    "Government Doon Medical College Hospital": (30.3244, 78.0437),
    "Synergy Institute of Medical Sciences": (30.3345, 78.0095),
    "Coronation District Hospital": (30.3290, 78.0565),
    "Graphic Era Hospital": (30.2690, 78.0090),
    "Himalayan Hospital": (30.1905, 78.1700),
    "AIIMS Rishikesh": (30.0869, 78.2676),
}


def fix_hospital_locations():
    import init_db
    moved = 0
    conn = db.get_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            for name, area, lat, lon, *_ in init_db.HOSPITALS:
                old = OLD_LOCATIONS.get(name)
                if not old:
                    continue
                moved += cur.execute(
                    "UPDATE hospitals SET area = %s, latitude = %s, longitude = %s "
                    "WHERE name = %s AND ABS(latitude - %s) < 0.00005 AND ABS(longitude - %s) < 0.00005",
                    (area, lat, lon, name, old[0], old[1]))
    finally:
        conn.close()
    if moved:
        print(f"moved {moved} hospitals to their OpenStreetMap locations")


if __name__ == "__main__":
    wait_for_mysql()
    if os.environ.get("RESET_DB") == "1" or not has_tables():
        import init_db
        print(f"creating database '{config.DB_NAME}' with demo data ...")
        init_db.initialise()
    else:
        print(f"database '{config.DB_NAME}' found, keeping its data (RESET_DB=1 to start fresh)")
        fix_hospital_locations()
    runpy.run_path(os.path.join(ROOT, "app.py"), run_name="__main__")
