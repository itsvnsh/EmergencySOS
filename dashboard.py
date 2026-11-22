"""
dashboard.py - numbers for the Dashboard tab (Tier 2).

Everything is computed from the live database in a few aggregate queries; the lab
hospital and lab requests are always left out.

    kpis              pending / active / done in 24 h / average real response time
    occupancy         beds and units in use per hospital (ambulances shown separately)
    cases_per_hour    SOS requests per hour over the last 24 h, critical (P1-P2) apart
    response_by_priority   real measured wait (SOS sent -> unit booked) per priority
    scheduling        the Scheduler Lab's simulation on the patients in the system:
                      average and critical wait per algorithm + the Automatic Gantt chart
    pool              connection-pool counters (db.py)
"""
import datetime as dt

import db
import scheduler

HOURS = 24
CRITICAL_MAX_PRIORITY = scheduler.CRITICAL_MAX_PRIORITY
CARE_TYPES = ["ICU_BED", "VENTILATOR", "OPERATION_THEATRE", "GENERAL_BED"]


def _f(v, nd=1):
    return round(float(v), nd) if v is not None else None


def kpis():
    row = db.query_one(f"""
        SELECT
          (SELECT COUNT(*) FROM sos_requests WHERE is_lab = 0 AND status = 'PENDING')   AS pending,
          (SELECT COUNT(*) FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id
             WHERE a.status = 'ACTIVE' AND r.is_lab = 0)                                AS active,
          (SELECT COUNT(*) FROM sos_requests WHERE is_lab = 0
             AND created_at >= NOW() - INTERVAL {HOURS} HOUR)                           AS sos_24h,
          (SELECT COUNT(*) FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id
             WHERE r.is_lab = 0 AND a.status = 'RELEASED'
             AND a.released_at >= NOW() - INTERVAL {HOURS} HOUR)                        AS done_24h,
          (SELECT AVG(TIMESTAMPDIFF(SECOND, r.created_at, a.allocated_at)) / 60
             FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id
             WHERE r.is_lab = 0 AND a.allocated_at >= NOW() - INTERVAL {HOURS} HOUR)    AS avg_response_min
    """)
    return {"pending": int(row["pending"]), "active": int(row["active"]), "sos_24h": int(row["sos_24h"]),
            "done_24h": int(row["done_24h"]), "avg_response_min": _f(row["avg_response_min"])}


def occupancy():
    rows = db.query_all("""
        SELECT h.hospital_id, h.name, r.resource_type, r.total_units, r.available_units
        FROM hospitals h JOIN resources r ON r.hospital_id = h.hospital_id
        WHERE h.is_lab = 0 ORDER BY h.hospital_id
    """)
    by_h = {}
    for r in rows:
        h = by_h.setdefault(r["hospital_id"], {"hospital_id": r["hospital_id"], "name": r["name"],
                                               "used": 0, "total": 0, "types": {},
                                               "ambulances_out": 0, "ambulances_total": 0})
        used = r["total_units"] - r["available_units"]
        if r["resource_type"] == "AMBULANCE":
            h["ambulances_out"], h["ambulances_total"] = used, r["total_units"]
            continue
        h["used"] += used
        h["total"] += r["total_units"]
        h["types"][r["resource_type"]] = {"used": used, "total": r["total_units"]}
    out = list(by_h.values())
    for h in out:
        h["pct"] = round(100 * h["used"] / h["total"], 1) if h["total"] else 0.0
    out.sort(key=lambda h: (-h["pct"], h["name"]))
    used, total = sum(h["used"] for h in out), sum(h["total"] for h in out)
    return {"hospitals": out, "used": used, "total": total,
            "pct": round(100 * used / total, 1) if total else 0.0}


def cases_per_hour(now=None):
    now = now or db.query_one("SELECT NOW() AS t")["t"]
    start = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=HOURS - 1)
    rows = db.query_all(f"""
        SELECT DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:00') AS hour,
               SUM(priority <= {CRITICAL_MAX_PRIORITY}) AS critical, COUNT(*) AS total
        FROM sos_requests WHERE is_lab = 0 AND created_at >= %s
        GROUP BY hour
    """, (start,))
    got = {r["hour"]: r for r in rows}
    out = []
    for i in range(HOURS):
        t = start + dt.timedelta(hours=i)
        key = t.strftime("%Y-%m-%d %H:00")
        r = got.get(key)
        total = int(r["total"]) if r else 0
        crit = int(r["critical"] or 0) if r else 0
        out.append({"hour": key, "label": t.strftime("%H:00"), "total": total,
                    "critical": crit, "other": total - crit})
    return out


def response_by_priority():
    rows = db.query_all("""
        SELECT r.priority, COUNT(*) AS cases,
               AVG(TIMESTAMPDIFF(SECOND, r.created_at, a.allocated_at)) / 60 AS avg_min,
               MAX(TIMESTAMPDIFF(SECOND, r.created_at, a.allocated_at)) / 60 AS max_min
        FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id
        WHERE r.is_lab = 0
        GROUP BY r.priority ORDER BY r.priority
    """)
    got = {int(r["priority"]): r for r in rows}
    return [{"priority": p, "cases": int(got[p]["cases"]) if p in got else 0,
             "avg_min": _f(got[p]["avg_min"]) if p in got else None,
             "max_min": _f(got[p]["max_min"]) if p in got else None} for p in range(1, 6)]


def scheduling(workload, source):
    if not workload:
        return {"source": source, "patients": 0, "policies": [], "gantt": None}
    results = scheduler.compare(workload)
    auto = next(r for r in results if r["policy"] == "AUTO")
    return {
        "source": source, "patients": len(workload),
        "policies": [{"policy": r["policy"], "name": r["policy_name"],
                      "avg_waiting": r["metrics"]["avg_waiting"],
                      "avg_critical_waiting": r["metrics"]["avg_critical_waiting"],
                      "max_waiting": r["metrics"]["max_waiting"],
                      "critical_count": r["metrics"]["critical_count"]} for r in results],
        "gantt": {"policy_name": auto["policy_name"], "segments": auto["gantt"],
                  "makespan": auto["metrics"]["makespan"]},
    }


def snapshot(workload, source):
    now = db.query_one("SELECT NOW() AS t")["t"]
    return {
        "generated_at": now,
        "kpis": kpis(),
        "occupancy": occupancy(),
        "cases_per_hour": cases_per_hour(now),
        "response_by_priority": response_by_priority(),
        "scheduling": scheduling(workload, source),
        "pool": db.pool_stats(),
    }
