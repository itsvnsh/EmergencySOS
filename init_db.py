"""
init_db.py - creates the EmergencySOS database, its tables and simulated data.

Usage:
    python init_db.py            # create database + tables + sample data

Hospital names are real Dehradun-region hospitals with APPROXIMATE locations.
All bed counts, equipment and doctors are SIMULATED (hospitals do not publish
live availability). Counts can be changed later from the admin console.
"""
import os

import config
from db import get_connection

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# (name, area, lat, lon, phone, {resource: (total, available)})
HOSPITALS = [
    ("Max Super Speciality Hospital", "Malsi, Mussoorie Road", 30.3735, 78.0758, "0135-7193000",
     {"ICU_BED": (20, 4), "VENTILATOR": (12, 3), "OPERATION_THEATRE": (6, 2), "GENERAL_BED": (120, 25)}),
    ("Shri Mahant Indiresh Hospital", "Patel Nagar", 30.3005, 78.0135, "0135-6672000",
     {"ICU_BED": (30, 6), "VENTILATOR": (15, 4), "OPERATION_THEATRE": (8, 3), "GENERAL_BED": (300, 40)}),
    ("Government Doon Medical College Hospital", "Parade Ground", 30.3244, 78.0437, "0135-2659355",
     {"ICU_BED": (25, 2), "VENTILATOR": (14, 1), "OPERATION_THEATRE": (6, 1), "GENERAL_BED": (400, 30)}),
    ("Synergy Institute of Medical Sciences", "Ballupur", 30.3345, 78.0095, "0135-2751600",
     {"ICU_BED": (14, 3), "VENTILATOR": (8, 2), "OPERATION_THEATRE": (4, 1), "GENERAL_BED": (100, 18)}),
    ("Coronation District Hospital", "Ghanta Ghar", 30.3290, 78.0565, "0135-2650085",
     {"ICU_BED": (8, 1), "VENTILATOR": (4, 1), "OPERATION_THEATRE": (3, 1), "GENERAL_BED": (150, 22)}),
    ("Graphic Era Hospital", "Clement Town", 30.2690, 78.0090, "0135-2643421",
     {"ICU_BED": (16, 5), "VENTILATOR": (10, 4), "OPERATION_THEATRE": (5, 2), "GENERAL_BED": (150, 35)}),
    ("Himalayan Hospital", "Jolly Grant", 30.1905, 78.1700, "0135-2471200",
     {"ICU_BED": (40, 8), "VENTILATOR": (20, 6), "OPERATION_THEATRE": (10, 3), "GENERAL_BED": (500, 60)}),
    ("AIIMS Rishikesh", "Virbhadra Road, Rishikesh", 30.0869, 78.2676, "0135-2462900",
     {"ICU_BED": (60, 10), "VENTILATOR": (30, 7), "OPERATION_THEATRE": (14, 4), "GENERAL_BED": (900, 90)}),
]

# simulated doctors: hospital index -> list of (name, specialization, on_shift, max_cases)
DOCTORS = {
    0: [("Dr. Rohit Sharma", "CARDIOLOGIST", 1, 3), ("Dr. Neha Rawat", "NEUROLOGIST", 1, 3),
        ("Dr. Aman Bisht", "TRAUMA_SURGEON", 1, 2), ("Dr. Kavita Negi", "PULMONOLOGIST", 0, 3),
        ("Dr. Sanjay Gupta", "GENERAL_PHYSICIAN", 1, 4)],
    1: [("Dr. Priya Joshi", "CARDIOLOGIST", 1, 3), ("Dr. Manoj Thapa", "TRAUMA_SURGEON", 1, 3),
        ("Dr. Ritu Saklani", "GYNECOLOGIST", 1, 3), ("Dr. Deepak Panwar", "BURN_SPECIALIST", 1, 2),
        ("Dr. Anjali Kandpal", "PULMONOLOGIST", 1, 3), ("Dr. Vivek Rana", "GENERAL_PHYSICIAN", 1, 5)],
    2: [("Dr. Suresh Chauhan", "GENERAL_PHYSICIAN", 1, 6), ("Dr. Meena Pant", "GYNECOLOGIST", 1, 4),
        ("Dr. Harish Uniyal", "TRAUMA_SURGEON", 1, 3), ("Dr. Pooja Semwal", "CARDIOLOGIST", 0, 3)],
    3: [("Dr. Arjun Mehta", "NEUROLOGIST", 1, 3), ("Dr. Shalini Dobhal", "CARDIOLOGIST", 1, 2),
        ("Dr. Karan Aswal", "GENERAL_PHYSICIAN", 1, 4)],
    4: [("Dr. Rekha Bhatt", "GENERAL_PHYSICIAN", 1, 5), ("Dr. Naveen Butola", "GYNECOLOGIST", 1, 3)],
    5: [("Dr. Ishaan Kapoor", "CARDIOLOGIST", 1, 3), ("Dr. Sneha Dimri", "PULMONOLOGIST", 1, 3),
        ("Dr. Gaurav Nautiyal", "TRAUMA_SURGEON", 1, 2), ("Dr. Tanvi Bahuguna", "GENERAL_PHYSICIAN", 1, 4),
        ("Dr. Rahul Juyal", "NEUROLOGIST", 0, 3)],
    6: [("Dr. Alok Mamgain", "CARDIOLOGIST", 1, 4), ("Dr. Divya Ghildiyal", "NEUROLOGIST", 1, 3),
        ("Dr. Pankaj Rautela", "TRAUMA_SURGEON", 1, 3), ("Dr. Sunita Kothari", "BURN_SPECIALIST", 1, 3),
        ("Dr. Yash Bhandari", "PULMONOLOGIST", 1, 3), ("Dr. Monika Chamoli", "GYNECOLOGIST", 1, 3),
        ("Dr. Hemant Raturi", "GENERAL_PHYSICIAN", 1, 5)],
    7: [("Dr. Vikram Singh", "CARDIOLOGIST", 1, 4), ("Dr. Nidhi Agarwal", "NEUROLOGIST", 1, 4),
        ("Dr. Rajesh Kumar", "TRAUMA_SURGEON", 1, 4), ("Dr. Ayesha Khan", "BURN_SPECIALIST", 1, 3),
        ("Dr. Mohit Verma", "PULMONOLOGIST", 1, 4), ("Dr. Lata Tiwari", "GYNECOLOGIST", 1, 3),
        ("Dr. Ashok Yadav", "GENERAL_PHYSICIAN", 1, 6)],
}

# hidden hospital used only by the concurrency lab (excluded from ranking)
LAB_HOSPITAL = ("Concurrency Lab Hospital", "Simulation only", 30.3165, 78.0322, None)


def _split_sql(text):
    """Split schema.sql into single statements (comments removed)."""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def create_database():
    conn = get_connection(database=False, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        conn.close()


def create_tables():
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        statements = _split_sql(f.read())
    conn = get_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


def load_sample_data():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            hospital_ids = []
            for name, area, lat, lon, phone, res in HOSPITALS:
                cur.execute("INSERT INTO hospitals (name, area, latitude, longitude, phone) "
                            "VALUES (%s,%s,%s,%s,%s)", (name, area, lat, lon, phone))
                hid = cur.lastrowid
                hospital_ids.append(hid)
                for rtype, (total, avail) in res.items():
                    cur.execute("INSERT INTO resources (hospital_id, resource_type, total_units, "
                                "available_units) VALUES (%s,%s,%s,%s)", (hid, rtype, total, avail))
            for idx, docs in DOCTORS.items():
                for dname, spec, on_shift, max_cases in docs:
                    cur.execute("INSERT INTO doctors (hospital_id, name, specialization, on_shift, "
                                "max_cases) VALUES (%s,%s,%s,%s,%s)",
                                (hospital_ids[idx], dname, spec, on_shift, max_cases))

            # lab hospital: one ICU bed, one cardiologist with plenty of capacity
            cur.execute("INSERT INTO hospitals (name, area, latitude, longitude, phone, is_lab) "
                        "VALUES (%s,%s,%s,%s,%s,1)", LAB_HOSPITAL)
            lab_id = cur.lastrowid
            cur.execute("INSERT INTO resources (hospital_id, resource_type, total_units, available_units) "
                        "VALUES (%s,'ICU_BED',1,1)", (lab_id,))
            cur.execute("INSERT INTO doctors (hospital_id, name, specialization, on_shift, max_cases) "
                        "VALUES (%s,'Dr. Lab Cardiologist','CARDIOLOGIST',1,50)", (lab_id,))

            cur.execute("INSERT INTO audit_log (event_type, details) VALUES ('SYSTEM', %s)",
                        (f"Database initialised with {len(HOSPITALS)} hospitals (simulated data)",))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialise():
    """Full reset: database, tables, data. Also used by the admin 'reset' button and tests."""
    create_database()
    create_tables()
    load_sample_data()


if __name__ == "__main__":
    print(f"Connecting to MySQL at {config.DB_HOST}:{config.DB_PORT} as '{config.DB_USER}' ...")
    initialise()
    print(f"Database '{config.DB_NAME}' ready: {len(HOSPITALS)} hospitals, "
          f"{sum(len(d) for d in DOCTORS.values())} doctors, plus the concurrency-lab hospital.")
