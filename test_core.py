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
os.environ["ROUTING_MODE"] = "off"          # tests never call the internet; routing tests mock it
os.environ["AUTO_COMPLETE"] = "0"           # tests run completion by hand

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
# Road routing (no network: the HTTP call is replaced by a fake)
# ---------------------------------------------------------------------------
class RoutingTests(unittest.TestCase):
    def setUp(self):
        import routing
        self.routing = routing
        routing.clear_cache()
        self.old_mode, self.old_get = config.ROUTING_MODE, routing._get_json
        config.ROUTING_MODE = "osrm"

    def tearDown(self):
        config.ROUTING_MODE = self.old_mode
        self.routing._get_json = self.old_get
        self.routing.clear_cache()

    def test_table_gives_road_distance(self):
        calls = []

        def fake(url):
            calls.append(url)
            self.assertIn("/table/v1/driving/78.000000,30.000000;", url)   # OSRM wants lon,lat
            return {"code": "Ok", "distances": [[0, 5200.0, 9100.0]], "durations": [[0, 600.0, 1200.0]]}
        self.routing._get_json = fake
        out = self.routing.road_distances(30.0, 78.0, [(30.01, 78.0), (30.02, 78.0)])
        self.assertEqual([round(o["km"], 1) for o in out], [5.2, 9.1])
        self.assertEqual([o["minutes"] for o in out], [10.0, 20.0])
        self.assertEqual({o["source"] for o in out}, {"ROAD"})
        self.routing.road_distances(30.0, 78.0, [(30.01, 78.0)])           # cached: no 2nd call
        self.assertEqual(len(calls), 1)

    def test_road_distance_changes_the_ranking(self):
        # hospital 2 is closer in a straight line, but hospital 1 is closer by road
        cands = [cand(1, 30.03, 78.0), cand(2, 30.02, 78.0), cand(3, 30.20, 78.0)]
        straight = score_hospitals(30.0, 78.0, "CARDIOLOGIST", cands)
        self.assertEqual(straight[0]["hospital_id"], 2)
        road = {1: {"km": 3.5, "minutes": 8, "source": "ROAD"}, 2: {"km": 12.0, "minutes": 30, "source": "ROAD"},
                3: {"km": 25.0, "minutes": 45, "source": "ROAD"}}
        ranked = score_hospitals(30.0, 78.0, "CARDIOLOGIST", cands, distances=road)
        self.assertEqual(ranked[0]["hospital_id"], 1)
        self.assertEqual(ranked[0]["distance_source"], "ROAD")
        self.assertEqual(ranked[0]["travel_min"], 8)

    def test_offline_fallback_and_backoff(self):
        calls = []

        def broken(url):
            calls.append(url)
            raise OSError("network unreachable")
        self.routing._get_json = broken
        out = self.routing.road_distances(30.0, 78.0, [(30.1, 78.0)])
        self.assertEqual(out[0]["source"], "ESTIMATE")
        self.assertAlmostEqual(out[0]["km"], 11.12 * config.ROAD_FACTOR, places=1)
        self.assertEqual(self.routing.status()["mode"], "ESTIMATE")
        self.routing.road_distances(30.0, 78.0, [(30.2, 78.0)])             # server marked down: not asked again
        self.assertEqual(len(calls), 1)

    def test_route_path_is_lat_lon(self):
        self.routing._get_json = lambda url: {"code": "Ok", "routes": [{
            "distance": 4000, "duration": 480,
            "geometry": {"type": "LineString", "coordinates": [[78.0, 30.0], [78.01, 30.005], [78.02, 30.01]]}}]}
        r = self.routing.route(30.0, 78.0, 30.01, 78.02)
        self.assertEqual(r["path"][0], [30.0, 78.0])
        self.assertEqual((r["km"], r["minutes"], r["source"]), (4.0, 8.0, "ROAD"))

    def test_routing_off_never_calls_network(self):
        config.ROUTING_MODE = "off"
        self.routing._get_json = lambda url: self.fail("network must not be used")
        self.assertEqual(self.routing.route(30, 78, 30.1, 78)["source"], "ESTIMATE")
        self.assertEqual(len(self.routing.route(30, 78, 30.1, 78)["path"]), 2)


def login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["user"]


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
        login(self.c, "dispatcher", "dispatch123")

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
        login(self.c, "admin", "admin123")
        r = self.c.post("/api/admin/resource", json={"hospital_id": 1, "resource_type": "ICU_BED",
                                                     "total_units": 2, "available_units": 5})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get("/api/queue?policy=XYZ").status_code, 400)

    def test_admin_and_labs(self):
        login(self.c, "admin", "admin123")
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



# ---------------------------------------------------------------------------
# Phase III: login and roles
# ---------------------------------------------------------------------------
SOS = {"patient_name": "P", "latitude": 30.32, "longitude": 78.04, "emergency_type": "CARDIAC", "severity": 5}


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class AuthTests(unittest.TestCase):
    def setUp(self):
        import init_db
        from app import app
        init_db.initialise()
        self.c = app.test_client()

    def test_passwords_are_hashed(self):
        import db
        row = db.query_one("SELECT password_hash FROM users WHERE username = 'admin'")
        self.assertNotIn("admin123", row["password_hash"])
        self.assertTrue(row["password_hash"].startswith(("scrypt:", "pbkdf2:")))

    def test_wrong_password(self):
        r = self.c.post("/api/login", json={"username": "admin", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(self.c.get("/api/me").get_json()["user"])

    def test_not_logged_in_gets_401(self):
        for method, url in [("get", "/api/queue"), ("post", "/api/dispatch"), ("get", "/api/hospitals"),
                            ("post", "/api/admin/resource"), ("post", "/api/admin/reset"),
                            ("get", "/api/map"), ("post", "/api/allocations/1/release")]:
            self.assertEqual(getattr(self.c, method)(url, json={}).status_code, 401, url)

    def test_roles_are_enforced(self):
        login(self.c, "patient", "patient123")
        self.assertEqual(self.c.post("/api/dispatch", json={}).status_code, 403)
        self.assertEqual(self.c.get("/api/queue").status_code, 403)
        login(self.c, "dispatcher", "dispatch123")
        self.assertEqual(self.c.get("/api/queue").status_code, 200)
        r = self.c.post("/api/admin/resource", json={"hospital_id": 1, "resource_type": "ICU_BED",
                                                     "total_units": 5, "available_units": 1})
        self.assertEqual(r.status_code, 403)
        login(self.c, "admin", "admin123")
        self.assertEqual(self.c.post("/api/dispatch", json={}).status_code, 403)   # admins don't dispatch

    def test_hospital_admin_limited_to_own_hospital(self):
        user = login(self.c, "maxadmin", "admin123")
        own = user["hospital_id"]
        body = {"resource_type": "ICU_BED", "total_units": 20, "available_units": 9}
        self.assertEqual(self.c.post("/api/admin/resource", json={**body, "hospital_id": own}).status_code, 200)
        self.assertEqual(self.c.post("/api/admin/resource", json={**body, "hospital_id": own + 1}).status_code, 403)
        self.assertEqual(self.c.post("/api/admin/reset").status_code, 403)          # only the system admin
        self.assertEqual(self.c.get("/api/admin/users").status_code, 403)

    def test_sos_is_open_and_tracked_by_token(self):
        r = self.c.post("/api/sos", json=SOS)                         # no login needed in an emergency
        self.assertEqual(r.status_code, 201)
        rid, token = r.get_json()["request"]["request_id"], r.get_json()["track_token"]
        self.assertNotIn("track_token", r.get_json()["request"])
        self.assertEqual(self.c.get(f"/api/sos/{rid}/status").status_code, 404)              # no token
        self.assertEqual(self.c.get(f"/api/sos/{rid}/status?token=0000000000000000").status_code, 404)
        st = self.c.get(f"/api/sos/{rid}/status?token={token}").get_json()
        self.assertEqual(st["request"]["status"], "PENDING")
        self.assertEqual(st["queue_position"], 1)
        other = self.c.application.test_client()
        login(other, "dispatcher", "dispatch123")
        other.post("/api/dispatch", json={})
        st = self.c.get(f"/api/sos/{rid}/status?token={token}").get_json()
        self.assertEqual(st["request"]["status"], "ALLOCATED")
        self.assertIn("hospital_name", st["allocation"])
        self.assertGreaterEqual(len(st["route"]["path"]), 2)

    def test_patient_sees_own_requests(self):
        login(self.c, "patient", "patient123")
        rid = self.c.post("/api/sos", json=SOS).get_json()["request"]["request_id"]
        mine = self.c.get("/api/my/requests").get_json()["requests"]
        self.assertEqual([m["request_id"] for m in mine], [rid])
        self.assertEqual(self.c.get(f"/api/sos/{rid}/status").status_code, 200)   # own login, no token

    def test_disabled_user_cannot_log_in(self):
        import db
        db.execute("UPDATE users SET is_active = 0 WHERE username = 'dispatcher'")
        r = self.c.post("/api/login", json={"username": "dispatcher", "password": "dispatch123"})
        self.assertEqual(r.status_code, 401)

    def test_pages(self):
        self.assertEqual(self.c.get("/").status_code, 302)                   # -> login
        self.assertEqual(self.c.get("/login").status_code, 200)
        self.assertEqual(self.c.get("/sos").status_code, 200)
        login(self.c, "patient", "patient123")
        self.assertIn("/sos", self.c.get("/").headers["Location"])          # patients go to the portal
        login(self.c, "dispatcher", "dispatch123")
        self.assertEqual(self.c.get("/").status_code, 200)


# ---------------------------------------------------------------------------
# Phase III: admin panel (hospitals, doctors, patients, users)
# ---------------------------------------------------------------------------
@unittest.skipUnless(DB_OK, "MySQL not reachable")
class AdminTests(unittest.TestCase):
    def setUp(self):
        import init_db
        from app import app
        init_db.initialise()
        self.c = app.test_client()
        login(self.c, "admin", "admin123")

    def test_doctor_add_edit_delete(self):
        r = self.c.post("/api/admin/hospitals/1/doctors",
                        json={"name": "Dr. New", "specialization": "NEUROLOGIST", "max_cases": 2})
        self.assertEqual(r.status_code, 201)
        did = r.get_json()["doctor_id"]
        r = self.c.put(f"/api/admin/doctors/{did}",
                       json={"name": "Dr. New Name", "specialization": "CARDIOLOGIST", "max_cases": 4, "on_shift": False})
        self.assertEqual(r.status_code, 200)
        import db
        d = db.query_one("SELECT * FROM doctors WHERE doctor_id = %s", (did,))
        self.assertEqual((d["name"], d["specialization"], d["max_cases"], d["on_shift"]),
                         ("Dr. New Name", "CARDIOLOGIST", 4, 0))
        self.assertEqual(self.c.delete(f"/api/admin/doctors/{did}").status_code, 200)
        self.assertEqual(self.c.put(f"/api/admin/doctors/{did}", json={"name": "x", "specialization": "CARDIOLOGIST"}).status_code, 404)
        r = self.c.post("/api/admin/hospitals/1/doctors", json={"name": "Dr. X", "specialization": "WIZARD"})
        self.assertEqual(r.status_code, 400)

    def test_busy_doctor_rules(self):
        import transaction_manager as tm
        from app import insert_request
        rid = insert_request("T", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0]
        out = tm.reserve(rid)
        did = out["doctor"]["doctor_id"]
        r = self.c.delete(f"/api/admin/doctors/{did}")
        self.assertEqual(r.status_code, 400)                                   # has an open case
        r = self.c.put(f"/api/admin/doctors/{did}", json={"name": "Dr", "specialization": "CARDIOLOGIST", "max_cases": 0})
        self.assertEqual(r.status_code, 400)

    def test_hospital_add_edit_delete(self):
        r = self.c.post("/api/admin/hospitals", json={
            "name": "Test Care Hospital", "area": "Test Area", "phone": "0135-0000", "latitude": 30.31,
            "longitude": 78.03, "units": {"ICU_BED": {"total": 5}, "GENERAL_BED": {"total": 20, "free": 10}}})
        self.assertEqual(r.status_code, 201)
        hid = r.get_json()["hospital_id"]
        import db
        units = {x["resource_type"]: (x["total_units"], x["available_units"])
                 for x in db.query_all("SELECT * FROM resources WHERE hospital_id = %s", (hid,))}
        self.assertEqual(units["ICU_BED"], (5, 5))
        self.assertEqual(units["GENERAL_BED"], (20, 10))
        self.assertEqual(units["VENTILATOR"], (0, 0))
        r = self.c.put(f"/api/admin/hospitals/{hid}", json={"name": "Test Care Hospital 2", "area": "B",
                                                            "latitude": 30.3, "longitude": 78.0})
        self.assertEqual(r.status_code, 200)
        dup = self.c.put(f"/api/admin/hospitals/{hid}", json={"name": "AIIMS Rishikesh", "area": "B",
                                                              "latitude": 30.3, "longitude": 78.0})
        self.assertEqual(dup.status_code, 400)
        self.assertEqual(self.c.delete(f"/api/admin/hospitals/{hid}").status_code, 200)

    def test_hospital_with_history_cannot_be_deleted(self):
        import transaction_manager as tm
        from app import insert_request
        out = tm.reserve(insert_request("T", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0])
        r = self.c.delete(f"/api/admin/hospitals/{out['hospital']['hospital_id']}")
        self.assertEqual(r.status_code, 400)

    def test_patient_edit_and_cancel(self):
        rid = self.c.post("/api/sos", json=SOS).get_json()["request"]["request_id"]
        r = self.c.put(f"/api/requests/{rid}", json={"patient_name": "Corrected Name", "phone": "999"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post(f"/api/requests/{rid}/cancel").status_code, 403)   # dispatcher's job
        login(self.c, "dispatcher", "dispatch123")
        self.assertEqual(self.c.post(f"/api/requests/{rid}/cancel").status_code, 200)
        self.assertEqual(self.c.post(f"/api/requests/{rid}/cancel").status_code, 400)   # already cancelled
        self.assertEqual(self.c.get("/api/queue").get_json()["queue"], [])

    def test_users(self):
        r = self.c.post("/api/admin/users", json={"username": "newdisp", "password": "secret1",
                                                  "full_name": "New Dispatcher", "role": "DISPATCHER"})
        self.assertEqual(r.status_code, 201)
        dup = self.c.post("/api/admin/users", json={"username": "newdisp", "password": "secret1",
                                                    "full_name": "X", "role": "DISPATCHER"})
        self.assertEqual(dup.status_code, 400)
        weak = self.c.post("/api/admin/users", json={"username": "abc", "password": "1",
                                                     "full_name": "X", "role": "PATIENT"})
        self.assertEqual(weak.status_code, 400)
        other = self.c.application.test_client()
        login(other, "newdisp", "secret1")
        uid = r.get_json()["user_id"]
        self.assertEqual(self.c.post(f"/api/admin/users/{uid}/active", json={"active": False}).status_code, 200)
        self.assertEqual(other.get("/api/queue").status_code, 401)                 # disabled -> logged out
        me = self.c.get("/api/me").get_json()["user"]["user_id"]
        self.assertEqual(self.c.post(f"/api/admin/users/{me}/active", json={"active": False}).status_code, 400)

    def test_audit_records_who(self):
        self.c.post("/api/admin/doctor/1/shift", json={"on_shift": False})
        log = self.c.get("/api/audit").get_json()["log"]
        self.assertEqual(log[0]["event_type"], "ADMIN_UPDATE")
        self.assertEqual(log[0]["username"], "admin")


# ---------------------------------------------------------------------------
# Phase III: automatic case completion, live updates, map, load
# ---------------------------------------------------------------------------
@unittest.skipUnless(DB_OK, "MySQL not reachable")
class LiveTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()

    def _allocate(self):
        import transaction_manager as tm
        from app import insert_request
        out = tm.reserve(insert_request("T", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0])
        self.assertEqual(out["status"], "allocated")
        return out

    def test_expected_end_is_set(self):
        import db
        out = self._allocate()
        row = db.query_one("SELECT TIMESTAMPDIFF(SECOND, allocated_at, expected_end_at) AS s, travel_min, "
                           "distance_source FROM allocations WHERE allocation_id = %s", (out["allocation_id"],))
        self.assertEqual(row["s"], round(13 * 60 * config.CASE_TIME_SCALE))      # cardiac sev 5 = 13 min
        self.assertEqual(row["distance_source"], "ESTIMATE")
        self.assertIsNotNone(row["travel_min"])

    def test_due_cases_complete_automatically(self):
        import db
        import transaction_manager as tm
        a, b = self._allocate(), self._allocate()
        res = db.query_one("SELECT available_units FROM resources WHERE resource_id = %s", (a["hospital"]["resource_id"],))
        db.execute("UPDATE allocations SET expected_end_at = NOW() - INTERVAL 1 SECOND WHERE allocation_id = %s",
                   (a["allocation_id"],))
        self.assertEqual(tm.complete_due_cases(), [a["allocation_id"]])        # only the due one
        row = db.query_one("SELECT status, release_mode FROM allocations WHERE allocation_id = %s", (a["allocation_id"],))
        self.assertEqual((row["status"], row["release_mode"]), ("RELEASED", "AUTO"))
        after = db.query_one("SELECT available_units FROM resources WHERE resource_id = %s", (a["hospital"]["resource_id"],))
        self.assertEqual(after["available_units"], res["available_units"] + 1)
        self.assertEqual(tm.complete_due_cases(), [])                           # nothing twice
        self.assertEqual(db.query_one("SELECT status FROM allocations WHERE allocation_id = %s",
                                      (b["allocation_id"],))["status"], "ACTIVE")

    def test_manual_and_auto_release_race(self):
        """Dispatcher clicks Release while the worker auto-completes the same case: exactly one wins."""
        import threading
        import db
        import transaction_manager as tm
        a = self._allocate()
        db.execute("UPDATE allocations SET expected_end_at = NOW() - INTERVAL 1 SECOND WHERE allocation_id = %s",
                   (a["allocation_id"],))
        results = []
        t1 = threading.Thread(target=lambda: results.append(tm.release(a["allocation_id"])["result"]))
        t2 = threading.Thread(target=lambda: results.extend(["released"] * len(tm.complete_due_cases())))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results), ["already_released", "released"] if len(results) == 2 else ["released"])
        doc = db.query_one("SELECT current_cases FROM doctors WHERE doctor_id = %s", (a["doctor"]["doctor_id"],))
        self.assertEqual(doc["current_cases"], 0)                               # decremented exactly once

    def test_change_id_moves_and_stream_sends_it(self):
        import db
        from app import app
        before = db.latest_change_id()
        self._allocate()
        self.assertGreater(db.latest_change_id(), before)
        resp = app.test_client().get("/api/stream", buffered=False)
        self.assertEqual(resp.mimetype, "text/event-stream")
        chunks = iter(resp.response)
        first = next(chunks)
        second = next(chunks)
        resp.close()
        self.assertIn(b"retry:", first if isinstance(first, bytes) else first.encode())
        text = second if isinstance(second, bytes) else second.encode()
        self.assertIn(b"event: change", text)
        self.assertIn(str(db.latest_change_id()).encode(), text)

    def test_map_data(self):
        from app import app
        self._allocate()
        c = app.test_client()
        login(c, "dispatcher", "dispatch123")
        c.post("/api/sos", json=SOS)
        m = c.get("/api/map").get_json()
        self.assertEqual(len(m["hospitals"]), 8)
        self.assertEqual(len(m["waiting"]), 1)
        self.assertEqual(len(m["active"]), 1)
        self.assertGreaterEqual(len(m["active"][0]["route"]["path"]), 2)

    def test_small_load_run_is_consistent(self):
        import load_test
        r = load_test.run_direct(clients=8, ops=4, quiet=True)
        self.assertEqual(r["operations"], 32)
        self.assertEqual(r["error_count"], 0, r["errors"])
        self.assertTrue(r["consistent"], r["problems"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
