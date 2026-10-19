"""
test_core.py - automated tests for EmergencySOS.

    python -m unittest -v test_core

Pure-Python tests (classifier, scheduler, ranking formula) always run.
Database tests (transactions, race, API) run against a SEPARATE database
called `<DB_NAME>_test`, so your demo data is never touched. They are
skipped automatically if MySQL is not reachable.
"""
import os
import unittest
from datetime import datetime, timedelta

# use a separate test database BEFORE importing anything that reads config
os.environ["DB_NAME"] = os.environ.get("DB_NAME", "emergencysos") + "_test"

import json

import classifier
import config
import scheduler
from ranking import haversine_km, score_hospitals

HERE = os.path.dirname(os.path.abspath(__file__))


def sample_workload():
    with open(os.path.join(HERE, "sample_workload.json"), encoding="utf-8") as f:
        return scheduler.workload_from_requests(json.load(f)["requests"])


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
class ClassifierTests(unittest.TestCase):
    def test_critical_cardiac(self):
        c = classifier.classify("cardiac", 5)
        self.assertEqual(c["priority"], 1)
        self.assertEqual(c["required_resource"], "ICU_BED")
        self.assertEqual(c["required_specialist"], "CARDIOLOGIST")
        self.assertEqual(c["est_handling_min"], 13)

    def test_life_threatening_bump(self):
        # severity 3 normally gives P3; stroke is life-threatening so it becomes P2
        self.assertEqual(classifier.classify("STROKE", 3)["priority"], 2)
        self.assertEqual(classifier.classify("BURN", 3)["priority"], 3)

    def test_resource_escalation(self):
        self.assertEqual(classifier.classify("RESPIRATORY", 5)["required_resource"], "VENTILATOR")
        self.assertEqual(classifier.classify("RESPIRATORY", 3)["required_resource"], "ICU_BED")
        self.assertEqual(classifier.classify("RESPIRATORY", 1)["required_resource"], "GENERAL_BED")
        self.assertEqual(classifier.classify("MATERNITY", 4)["required_resource"], "OPERATION_THEATRE")
        self.assertEqual(classifier.classify("TRAUMA", 2)["required_resource"], "GENERAL_BED")

    def test_priority_always_in_range(self):
        for t in classifier.EMERGENCY_TYPES:
            for s in range(1, 6):
                c = classifier.classify(t, s)
                self.assertTrue(1 <= c["priority"] <= 5)
                self.assertGreater(c["est_handling_min"], 0)

    def test_invalid_input(self):
        with self.assertRaises(classifier.ClassificationError):
            classifier.classify("ALIEN", 3)
        with self.assertRaises(classifier.ClassificationError):
            classifier.classify("CARDIAC", 9)
        with self.assertRaises(classifier.ClassificationError):
            classifier.classify("CARDIAC", "high")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.wl = sample_workload()

    def test_all_requests_finish(self):
        for res in scheduler.compare(self.wl):
            self.assertEqual(len(res["results"]), 12)
            self.assertEqual(sorted(r["id"] for r in res["results"]), list(range(1, 13)))
            for r in res["results"]:
                self.assertGreaterEqual(r["waiting"], 0)
                self.assertEqual(r["turnaround"], r["waiting"] + r["burst"])

    def test_priority_beats_fcfs_for_critical(self):
        pr = scheduler.simulate(self.wl, "PRIORITY")["metrics"]
        fc = scheduler.simulate(self.wl, "FCFS")["metrics"]
        self.assertLess(pr["avg_critical_waiting"], fc["avg_critical_waiting"])

    def test_report_figures(self):
        """The numbers quoted in the Phase-II report."""
        self.assertEqual(scheduler.simulate(self.wl, "PRIORITY")["metrics"]["avg_critical_waiting"], 18.75)
        self.assertEqual(scheduler.simulate(self.wl, "FCFS")["metrics"]["avg_critical_waiting"], 27.88)

    def test_aging_lowers_longest_wait(self):
        aged = scheduler.simulate(self.wl, "PRIORITY")["metrics"]["max_waiting"]
        pure = scheduler.simulate(self.wl, "PRIORITY_NO_AGING")["metrics"]["max_waiting"]
        self.assertLess(aged, pure)

    def test_fcfs_order_is_arrival_order(self):
        order = [g["id"] for g in scheduler.simulate(self.wl, "FCFS")["gantt"] if g["id"]]
        self.assertEqual(order, list(range(1, 13)))

    def test_sjf_picks_shortest_ready_job(self):
        wl = [{"id": 1, "arrival": 0, "priority": 3, "burst": 5},
              {"id": 2, "arrival": 1, "priority": 3, "burst": 9},
              {"id": 3, "arrival": 1, "priority": 3, "burst": 2}]
        order = [g["id"] for g in scheduler.simulate(wl, "SJF")["gantt"] if g["id"]]
        self.assertEqual(order, [1, 3, 2])

    def test_idle_gap_recorded(self):
        wl = [{"id": 1, "arrival": 5, "priority": 1, "burst": 3}]
        gantt = scheduler.simulate(wl, "FCFS")["gantt"]
        self.assertEqual(gantt[0]["label"], "idle")
        self.assertEqual(gantt[1]["start"], 5)

    def test_effective_priority(self):
        self.assertEqual(scheduler.effective_priority(5, 0, 10), 5)
        self.assertEqual(scheduler.effective_priority(5, 25, 10), 3)
        self.assertEqual(scheduler.effective_priority(2, 100, 10), 1)   # never below 1
        self.assertEqual(scheduler.effective_priority(4, 100, 0), 4)    # aging disabled

    def test_live_queue_order(self):
        now = datetime(2026, 10, 16, 12, 0)
        reqs = [
            {"request_id": 1, "priority": 5, "est_handling_min": 5, "created_at": now - timedelta(minutes=45)},
            {"request_id": 2, "priority": 1, "est_handling_min": 13, "created_at": now - timedelta(minutes=1)},
            {"request_id": 3, "priority": 3, "est_handling_min": 8, "created_at": now - timedelta(minutes=2)},
        ]
        fcfs = [r["request_id"] for r in scheduler.order_queue(reqs, "FCFS", now)]
        prio = [r["request_id"] for r in scheduler.order_queue(reqs, "PRIORITY", now)]
        sjf = [r["request_id"] for r in scheduler.order_queue(reqs, "SJF", now)]
        self.assertEqual(fcfs, [1, 3, 2])
        self.assertEqual(prio, [2, 1, 3])      # #1 aged from P5 to P1 but genuine P1 still wins the tie
        self.assertEqual(sjf, [1, 3, 2])

    def test_auto_mode_picks_algorithm_by_severity(self):
        """Arnav's example: fever, burn, cardiac arrest and accident waiting together."""
        now = datetime(2026, 10, 16, 12, 0)
        reqs = [
            {"request_id": 1, "priority": 5, "est_handling_min": 6, "created_at": now - timedelta(minutes=4)},   # fever
            {"request_id": 2, "priority": 3, "est_handling_min": 12, "created_at": now - timedelta(minutes=3)},  # burn
            {"request_id": 3, "priority": 1, "est_handling_min": 13, "created_at": now - timedelta(minutes=2)},  # cardiac arrest
            {"request_id": 4, "priority": 2, "est_handling_min": 9, "created_at": now - timedelta(minutes=1)},   # accident
            {"request_id": 5, "priority": 3, "est_handling_min": 8, "created_at": now},                           # short urgent
        ]
        q = scheduler.order_queue(reqs, "AUTO", now)
        self.assertEqual([r["request_id"] for r in q], [3, 4, 5, 2, 1])
        self.assertEqual(q[0]["auto"]["algorithm"], "Priority with aging")      # critical queue
        self.assertEqual(q[2]["auto"]["algorithm"], "Shortest Job First")       # urgent: 8 min before 12 min
        self.assertEqual(q[4]["auto"]["algorithm"], "First Come First Served")  # routine

    def test_auto_mode_aging_moves_patient_up(self):
        now = datetime(2026, 10, 16, 12, 0)
        reqs = [
            {"request_id": 1, "priority": 5, "est_handling_min": 6, "created_at": now - timedelta(minutes=30)},
            {"request_id": 2, "priority": 3, "est_handling_min": 8, "created_at": now},
        ]
        q = scheduler.order_queue(reqs, "AUTO", now)
        self.assertEqual(q[0]["request_id"], 1)            # P5 aged to P2 -> critical queue
        self.assertEqual(q[0]["auto"]["queue"], "Critical")
        self.assertIn("aging", q[0]["auto"]["reason"])

    def test_unknown_policy(self):
        with self.assertRaises(ValueError):
            scheduler.simulate(self.wl, "ROUND_ROBIN")


# ---------------------------------------------------------------------------
# Ranking formula
# ---------------------------------------------------------------------------
def cand(hid, lat, lon, free=3, total=10, spec=1, phys=1, all_total=100, all_free=30):
    return {"hospital_id": hid, "name": f"H{hid}", "resource_id": hid, "latitude": lat, "longitude": lon,
            "available_units": free, "total_units": total, "specialists_free": spec,
            "physicians_free": phys, "all_total": all_total, "all_free": all_free}


class RankingTests(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(config.RANK_WEIGHTS.values()), 1.0)

    def test_haversine(self):
        self.assertAlmostEqual(haversine_km(30.0, 78.0, 30.0, 78.0), 0.0)
        # 0.1 degree of latitude is about 11.1 km
        self.assertAlmostEqual(haversine_km(30.0, 78.0, 30.1, 78.0), 11.12, places=1)

    def test_nearer_hospital_wins_when_equal(self):
        ranked = score_hospitals(30.0, 78.0, "CARDIOLOGIST",
                                 [cand(1, 30.20, 78.0), cand(2, 30.01, 78.0)])
        self.assertEqual(ranked[0]["hospital_id"], 2)

    def test_full_hospital_excluded(self):
        ranked = score_hospitals(30.0, 78.0, "CARDIOLOGIST",
                                 [cand(1, 30.01, 78.0, free=0), cand(2, 30.2, 78.0)])
        self.assertEqual([r["hospital_id"] for r in ranked], [2])

    def test_specialist_can_beat_distance(self):
        near_no_doc = cand(1, 30.02, 78.0, spec=0, phys=0)
        slightly_farther_with_doc = cand(2, 30.03, 78.0, spec=1)
        far_away = cand(3, 30.50, 78.0, spec=1)       # sets the distance scale
        ranked = score_hospitals(30.0, 78.0, "CARDIOLOGIST",
                                 [near_no_doc, slightly_farther_with_doc, far_away])
        self.assertEqual(ranked[0]["hospital_id"], 2)

    def test_scores_in_range(self):
        ranked = score_hospitals(30.0, 78.0, "NEUROLOGIST",
                                 [cand(i, 30 + i / 50, 78.0, free=i) for i in range(1, 6)])
        for r in ranked:
            self.assertTrue(0 <= r["score"] <= 100)


# ---------------------------------------------------------------------------
# Database: transactions, race, API
# ---------------------------------------------------------------------------
def _db_available():
    try:
        import init_db
        init_db.initialise()
        return True
    except Exception as e:   # pragma: no cover
        print(f"\n[skip] database tests skipped: {e}")
        return False


DB_OK = _db_available()


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()

    def test_check_constraint_blocks_negative_units(self):
        import pymysql
        import db
        with self.assertRaises(pymysql.err.OperationalError):
            db.execute("UPDATE resources SET available_units = -1 WHERE resource_id = 1")

    def test_race_unlocked_overbooks(self):
        import concurrency_lab
        r = concurrency_lab.run_race(6, locked=False, delay_ms=50)
        self.assertEqual(r["bookings"], 6)
        self.assertFalse(r["consistent"])

    def test_race_locked_books_once(self):
        import concurrency_lab
        for delay in (50, 0):     # correct with or without the artificial delay
            r = concurrency_lab.run_race(6, locked=True, delay_ms=delay)
            self.assertEqual(r["bookings"], 1)
            self.assertEqual(r["rolled_back"], 5)
            self.assertEqual(r["final_available"], 0)
            self.assertTrue(r["consistent"])

    def _new_request(self, etype="CARDIAC", severity=5, lat=30.2683, lon=77.9947):
        from app import insert_request
        return insert_request("Test Patient", None, lat, lon, etype, severity, None)[0]

    def test_stale_ranking_falls_back(self):
        import db
        import ranking
        import transaction_manager as tm
        rid = self._new_request()
        req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (rid,))
        ranked = ranking.rank_for_request(req)              # snapshot
        best = ranked[0]
        db.execute("UPDATE resources SET available_units = 0 WHERE resource_id = %s",
                   (best["resource_id"],))                   # best hospital fills up meanwhile
        out = tm.reserve(rid, ranked=ranked)
        self.assertEqual(out["status"], "allocated")
        self.assertEqual(out["attempts"][0]["result"], "no_unit")
        self.assertNotEqual(out["hospital"]["hospital_id"], best["hospital_id"])
        self.assertEqual(out["fallbacks"], 1)

    def test_reserve_then_release_restores_counters(self):
        import db
        import transaction_manager as tm
        rid = self._new_request()
        out = tm.reserve(rid)
        self.assertEqual(out["status"], "allocated")
        res_id = out["hospital"]["resource_id"]
        before = db.query_one("SELECT available_units FROM resources WHERE resource_id=%s", (res_id,))
        doc_before = db.query_one("SELECT current_cases FROM doctors WHERE doctor_id=%s",
                                  (out["doctor"]["doctor_id"],))
        self.assertEqual(doc_before["current_cases"], 1)
        self.assertEqual(tm.release(out["allocation_id"])["result"], "released")
        after = db.query_one("SELECT available_units FROM resources WHERE resource_id=%s", (res_id,))
        self.assertEqual(after["available_units"], before["available_units"] + 1)
        self.assertEqual(tm.release(out["allocation_id"])["result"], "already_released")
        status = db.query_one("SELECT status FROM sos_requests WHERE request_id=%s", (rid,))["status"]
        self.assertEqual(status, "COMPLETED")

    def test_no_double_allocation_of_same_request(self):
        import transaction_manager as tm
        rid = self._new_request()
        self.assertEqual(tm.reserve(rid)["status"], "allocated")
        self.assertEqual(tm.reserve(rid)["status"], "not_pending")

    def test_no_capacity_keeps_request_pending(self):
        import db
        import transaction_manager as tm
        db.execute("UPDATE resources SET available_units = 0 WHERE resource_type = 'VENTILATOR'")
        rid = self._new_request("RESPIRATORY", 5)
        self.assertEqual(tm.reserve(rid)["status"], "no_capacity")
        self.assertEqual(db.query_one("SELECT status FROM sos_requests WHERE request_id=%s",
                                      (rid,))["status"], "PENDING")


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class ApiTests(unittest.TestCase):
    def setUp(self):
        import init_db
        from app import app
        init_db.initialise()
        self.c = app.test_client()

    def test_full_flow(self):
        r = self.c.post("/api/sos", json={"patient_name": "A", "latitude": 30.32, "longitude": 78.04,
                                          "emergency_type": "TRAUMA", "severity": 4})
        self.assertEqual(r.status_code, 201)
        self.c.post("/api/sos", json={"patient_name": "B", "latitude": 30.32, "longitude": 78.04,
                                      "emergency_type": "CARDIAC", "severity": 5})
        q = self.c.get("/api/queue?policy=PRIORITY").get_json()["queue"]
        self.assertEqual(q[0]["patient_name"], "B")                 # P1 before P2
        q = self.c.get("/api/queue?policy=FCFS").get_json()["queue"]
        self.assertEqual(q[0]["patient_name"], "A")
        d = self.c.post("/api/dispatch", json={}).get_json()            # Automatic mode by default
        self.assertEqual(d["status"], "allocated")
        self.assertEqual(d["request"]["patient_name"], "B")
        self.assertEqual(d["decision"]["queue"], "Critical")
        allocs = self.c.get("/api/allocations").get_json()["allocations"]
        self.assertEqual(len(allocs), 1)
        rel = self.c.post(f"/api/allocations/{allocs[0]['allocation_id']}/release")
        self.assertEqual(rel.status_code, 200)

    def test_validation(self):
        self.assertEqual(self.c.post("/api/sos", json={"patient_name": ""}).status_code, 400)
        r = self.c.post("/api/sos", json={"patient_name": "X", "latitude": 30, "longitude": 78,
                                          "emergency_type": "CARDIAC", "severity": 7})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/admin/resource", json={"hospital_id": 1, "resource_type": "ICU_BED",
                                                     "total_units": 2, "available_units": 5})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get("/api/queue?policy=XYZ").status_code, 400)

    def test_admin_and_labs(self):
        r = self.c.post("/api/admin/resource", json={"hospital_id": 1, "resource_type": "ICU_BED",
                                                     "total_units": 22, "available_units": 7})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post("/api/admin/doctor/1/shift", json={"on_shift": False}).status_code, 200)
        lab = self.c.post("/api/labs/scheduler", json={"source": "sample"}).get_json()
        self.assertEqual(len(lab["results"]), 6)
        race = self.c.post("/api/labs/race", json={"locked": True}).get_json()
        self.assertEqual(race["bookings"], 1)
        self.assertGreater(len(self.c.get("/api/audit").get_json()["log"]), 0)

    def test_lab_on_real_patients(self):
        # no patients yet -> friendly error
        self.assertEqual(self.c.post("/api/labs/scheduler", json={"source": "real"}).status_code, 400)
        for name, etype, sev in [("Fever", "GENERAL", 2), ("Heart", "CARDIAC", 5), ("Burn", "BURN", 3)]:
            self.c.post("/api/sos", json={"patient_name": name, "latitude": 30.32, "longitude": 78.04,
                                          "emergency_type": etype, "severity": sev})
        self.c.post("/api/dispatch", json={})                     # Heart gets an active allocation
        wl = self.c.get("/api/labs/scheduler/workload?source=real").get_json()["workload"]
        self.assertEqual(sorted(w["patient_name"] for w in wl), ["Burn", "Fever", "Heart"])
        self.assertIn("ACTIVE ALLOCATION", [w["status"] for w in wl])
        lab = self.c.post("/api/labs/scheduler", json={"source": "real"}).get_json()
        auto = next(r for r in lab["results"] if r["policy"] == "AUTO")
        first = next(g for g in auto["gantt"] if g["id"])
        self.assertEqual(next(w["patient_name"] for w in wl if w["id"] == first["id"]), "Heart")

    def test_empty_queue_dispatch(self):
        d = self.c.post("/api/dispatch", json={"policy": "FCFS"}).get_json()
        self.assertEqual(d["status"], "empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
