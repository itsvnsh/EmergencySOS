"""
setup_mysql.py - one-time MySQL setup for EmergencySOS.

Creates the two databases the project uses and a dedicated MySQL user for the
app, so the app itself never needs the root/admin password:

    databases : emergencysos, emergencysos_test
    user      : sos@localhost   (password of your choice, default sos123)

Usage:
    python setup_mysql.py

You are asked for an ADMIN login once (root, or whoever can create users).
If you use XAMPP, root has no password: just press Enter when asked.
The admin password is only used for this run and is never saved.

At the end the app login is saved to db_login.json, which config.py reads
automatically, so `python init_db.py` and `python app.py` just work.
"""
import getpass
import json
import os
import re
import sys

try:
    import pymysql
except ImportError:
    sys.exit("PyMySQL is not installed. Run:  pip install -r requirements.txt")

HERE = os.path.dirname(os.path.abspath(__file__))
LOGIN_FILE = os.path.join(HERE, "db_login.json")
DATABASES = ["emergencysos", "emergencysos_test"]


def ask(prompt, default):
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def connect(host, port, user, password, database=None):
    return pymysql.connect(host=host, port=port, user=user, password=password,
                           database=database, charset="utf8mb4", connect_timeout=5)


def explain(error):
    code = error.args[0] if error.args else None
    if code == 1045:
        return "wrong username or password"
    if code == 2003:
        return "cannot reach MySQL - is the MySQL service (or XAMPP MySQL) running?"
    if code in (1044, 1227, 1410):
        return "this account is not allowed to create databases/users - use root or ask the admin"
    return str(error)


def main():
    print("EmergencySOS - MySQL setup\n")
    host = ask("MySQL host", "localhost")
    port = int(ask("MySQL port", "3306"))

    # ---- 1. admin login (try root with no password first, e.g. XAMPP) ----
    admin_user = ask("Admin user", "root")
    admin = None
    if admin_user == "root":
        try:
            admin = connect(host, port, "root", "")
            print("Connected as root (no password).")
        except pymysql.err.OperationalError as e:
            if e.args and e.args[0] == 2003:
                sys.exit(f"\nERROR: {explain(e)}")
    while admin is None:
        admin_pw = getpass.getpass(f"Password for {admin_user} (input hidden): ")
        try:
            admin = connect(host, port, admin_user, admin_pw)
        except pymysql.err.OperationalError as e:
            print(f"  Could not log in: {explain(e)}")
            if e.args and e.args[0] == 2003:
                sys.exit(1)
            if input("  Try again? [Y/n]: ").strip().lower() == "n":
                sys.exit("Stopped. If you forgot the root password, see the README troubleshooting.")

    # ---- 2. app user details ----
    app_user = ask("New app user to create", "sos")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", app_user):
        sys.exit("User name may only contain letters, digits and _ (max 32).")
    app_pw = getpass.getpass("Password for the app user (Enter = sos123): ") or "sos123"

    # ---- 3. create databases, user, grants ----
    try:
        with admin.cursor() as cur:
            for db in DATABASES:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` "
                            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                print(f"  database {db}: ok")
            cur.execute("CREATE USER IF NOT EXISTS %s@'localhost' IDENTIFIED BY %s", (app_user, app_pw))
            cur.execute("ALTER USER %s@'localhost' IDENTIFIED BY %s", (app_user, app_pw))   # if it existed
            for db in DATABASES:
                cur.execute(f"GRANT ALL PRIVILEGES ON `{db}`.* TO %s@'localhost'", (app_user,))
            cur.execute("FLUSH PRIVILEGES")
            print(f"  user {app_user}@localhost: ok (full rights on {', '.join(DATABASES)} only)")
        admin.commit()
    except pymysql.err.MySQLError as e:
        sys.exit(f"\nERROR while creating databases/user: {explain(e)}")
    finally:
        admin.close()

    # ---- 4. check the new login really works ----
    try:
        connect(host, port, app_user, app_pw, DATABASES[0]).close()
    except pymysql.err.MySQLError as e:
        sys.exit(f"\nERROR: the new user could not log in: {explain(e)}")
    print("  login test as the new user: ok")

    # ---- 5. save the app login for config.py ----
    with open(LOGIN_FILE, "w", encoding="utf-8") as f:
        json.dump({"DB_HOST": host, "DB_PORT": port, "DB_USER": app_user, "DB_PASSWORD": app_pw}, f, indent=2)

    print(f"""
Done. App login saved to db_login.json (keep it out of Git - it is in .gitignore).

Next steps (Python version):
    python init_db.py
    python app.py          then open http://127.0.0.1:5000

Java version: start it with this login instead of root:
    PowerShell:  $env:DB_USER="{app_user}"; $env:DB_PASSWORD="<the password>"; mvn spring-boot:run
""")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
