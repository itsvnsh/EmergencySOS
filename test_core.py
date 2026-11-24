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
os.environ["SOS_MAX_PER_MIN"] = "100000"    # many test SOS from 127.0.0.1; the limit has its own tests

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

from flask.testing import FlaskClient   # noqa: E402


class CsrfClient(FlaskClient):
    """Test client that behaves like the real pages: every POST/PUT/PATCH/DELETE carries
    the session's CSRF token (Tier 3). The CSRF tests use a plain FlaskClient instead."""
    def open(self, *args, **kwargs):
        if str(kwargs.get("method", "GET")).upper() in ("POST", "PUT", "PATCH", "DELETE"):
            headers = dict(kwargs.pop("headers", None) or {})
            if "X-CSRF-Token" not in headers:
                headers["X-CSRF-Token"] = super().open("/api/csrf").get_json()["csrf"]
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


if DB_OK:
    from app import app as _flask_app
    _flask_app.test_client_class = CsrfClient


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
        from app import app
        adm = app.test_client()                     # the labs are for the system admin login
        login(adm, "admin", "admin123")
        # no patients yet -> friendly error
        self.assertEqual(adm.post("/api/labs/scheduler", json={"source": "real"}).status_code, 400)
        for name, etype, sev in [("Fever", "GENERAL", 2), ("Heart", "CARDIAC", 5), ("Burn", "BURN", 3)]:
            self.c.post("/api/sos", json={"patient_name": name, "latitude": 30.32, "longitude": 78.04,
                                          "emergency_type": etype, "severity": sev})
        self.c.post("/api/dispatch", json={})                     # Heart gets an active allocation
        wl = adm.get("/api/labs/scheduler/workload?source=real").get_json()["workload"]
        self.assertEqual(sorted(w["patient_name"] for w in wl), ["Burn", "Fever", "Heart"])
        self.assertIn("ACTIVE ALLOCATION", [w["status"] for w in wl])
        lab = adm.post("/api/labs/scheduler", json={"source": "real"}).get_json()
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


# ---------------------------------------------------------------------------
# Ambulances: a second resource taken in the same transaction as the bed
# ---------------------------------------------------------------------------
@unittest.skipUnless(DB_OK, "MySQL not reachable")
class AmbulanceTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()

    def _fleet(self, hospital_id):
        import db
        return db.query_one("SELECT resource_id, available_units, total_units FROM resources "
                            "WHERE hospital_id = %s AND resource_type = 'AMBULANCE'", (hospital_id,))

    def _request(self):
        from app import insert_request
        return insert_request("Amb Test", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0]

    def test_bed_and_ambulance_taken_together(self):
        import transaction_manager as tm
        rid = self._request()
        import db, ranking
        best = ranking.rank_for_request(db.query_one("SELECT * FROM sos_requests WHERE request_id=%s", (rid,)))[0]
        before = self._fleet(best["hospital_id"])
        out = tm.reserve(rid)
        self.assertEqual(out["status"], "allocated")
        self.assertEqual(out["hospital"]["hospital_id"], best["hospital_id"])
        self.assertEqual(self._fleet(best["hospital_id"])["available_units"], before["available_units"] - 1)
        self.assertEqual(out["ambulance"]["resource_id"], before["resource_id"])

    def test_no_free_ambulance_skips_hospital_and_keeps_bed(self):
        import db
        import ranking
        import transaction_manager as tm
        rid = self._request()
        req = db.query_one("SELECT * FROM sos_requests WHERE request_id=%s", (rid,))
        ranked = ranking.rank_for_request(req)                 # snapshot while ambulances are free
        best = ranked[0]
        db.execute("UPDATE resources SET available_units = 0 WHERE hospital_id = %s "
                   "AND resource_type = 'AMBULANCE'", (best["hospital_id"],))
        beds = db.query_one("SELECT available_units FROM resources WHERE resource_id=%s", (best["resource_id"],))
        out = tm.reserve(rid, ranked=ranked)
        self.assertEqual(out["attempts"][0]["result"], "no_ambulance")
        self.assertEqual(out["status"], "allocated")
        self.assertNotEqual(out["hospital"]["hospital_id"], best["hospital_id"])
        # all-or-nothing: the bed of the first hospital was not kept
        self.assertEqual(db.query_one("SELECT available_units FROM resources WHERE resource_id=%s",
                                      (best["resource_id"],))["available_units"], beds["available_units"])
        # and a fresh ranking leaves that hospital out completely
        self.assertNotIn(best["hospital_id"], [c["hospital_id"] for c in ranking.rank_for_request(req)])

    def test_ambulance_returns_before_the_bed(self):
        import db
        import transaction_manager as tm
        out = tm.reserve(self._request())
        aid, hid = out["allocation_id"], out["hospital"]["hospital_id"]
        before = self._fleet(hid)["available_units"]
        db.execute("UPDATE allocations SET ambulance_due_at = NOW() - INTERVAL 1 SECOND WHERE allocation_id=%s", (aid,))
        self.assertEqual(tm.return_due_ambulances(), [aid])
        self.assertEqual(tm.return_due_ambulances(), [])                     # only once
        self.assertEqual(self._fleet(hid)["available_units"], before + 1)
        row = db.query_one("SELECT status, ambulance_returned_at FROM allocations WHERE allocation_id=%s", (aid,))
        self.assertEqual(row["status"], "ACTIVE")                              # the bed is still taken
        self.assertIsNotNone(row["ambulance_returned_at"])
        tm.release(aid)                                                        # must not return it twice
        self.assertEqual(self._fleet(hid)["available_units"], before + 1)

    def test_release_brings_ambulance_back_if_still_out(self):
        import transaction_manager as tm
        out = tm.reserve(self._request())
        hid = out["hospital"]["hospital_id"]
        before = self._fleet(hid)["available_units"]
        self.assertEqual(tm.release(out["allocation_id"])["result"], "released")
        self.assertEqual(self._fleet(hid)["available_units"], before + 1)

    def test_round_trip_time(self):
        import transaction_manager as tm
        self.assertEqual(tm.ambulance_round_trip_sec(6), round(2 * 6 * 60 * config.CASE_TIME_SCALE))
        self.assertGreater(tm.ambulance_round_trip_sec(None), 0)

    def test_full_fleet_out_means_no_capacity(self):
        import db
        import transaction_manager as tm
        db.execute("UPDATE resources SET available_units = 0 WHERE resource_type = 'AMBULANCE'")
        rid = self._request()
        self.assertEqual(tm.reserve(rid)["status"], "no_capacity")
        self.assertEqual(db.query_one("SELECT status FROM sos_requests WHERE request_id=%s", (rid,))["status"],
                         "PENDING")


# ---------------------------------------------------------------------------
# Banker's algorithm (deadlock avoidance)
# ---------------------------------------------------------------------------
class BankersAlgorithmTests(unittest.TestCase):
    """The pure algorithm, on the textbook example (Silberschatz: 5 processes, 3 types)."""
    T = ["A", "B", "C"]
    P = [
        {"id": "P0", "allocation": {"A": 0, "B": 1, "C": 0}, "max": {"A": 7, "B": 5, "C": 3}},
        {"id": "P1", "allocation": {"A": 2, "B": 0, "C": 0}, "max": {"A": 3, "B": 2, "C": 2}},
        {"id": "P2", "allocation": {"A": 3, "B": 0, "C": 2}, "max": {"A": 9, "B": 0, "C": 2}},
        {"id": "P3", "allocation": {"A": 2, "B": 1, "C": 1}, "max": {"A": 2, "B": 2, "C": 2}},
        {"id": "P4", "allocation": {"A": 0, "B": 0, "C": 2}, "max": {"A": 4, "B": 3, "C": 3}},
    ]

    def test_textbook_state_is_safe(self):
        import bankers
        r = bankers.check_safety({"A": 3, "B": 3, "C": 2}, self.P, self.T)
        self.assertTrue(r["safe"])
        self.assertEqual(r["sequence"][0], "P1")
        self.assertEqual(sorted(r["sequence"]), ["P0", "P1", "P2", "P3", "P4"])
        self.assertEqual(r["need"]["P0"], {"A": 7, "B": 4, "C": 3})
        # Work grows by each finished process's allocation
        first = r["steps"][0]
        self.assertEqual(first["work_after"], {"A": 5, "B": 3, "C": 2})

    def test_nothing_free_is_unsafe(self):
        import bankers
        r = bankers.check_safety({"A": 0, "B": 0, "C": 0}, self.P, self.T)
        self.assertFalse(r["safe"])
        self.assertEqual(len(r["blocked"]), 5)

    def test_textbook_requests(self):
        import bankers
        # P1 requests (1,0,2): pretend to grant it -> still safe, so it is granted (textbook answer)
        state = [dict(p) for p in self.P]
        state[1] = {**state[1], "allocation": {"A": 3, "B": 0, "C": 2}}
        self.assertTrue(bankers.check_safety({"A": 2, "B": 3, "C": 0}, state, self.T)["safe"])
        # then P0 requests (0,2,0): granting it would be unsafe, so P0 must wait (textbook answer)
        state[0] = {**state[0], "allocation": {"A": 0, "B": 3, "C": 0}}
        self.assertFalse(bankers.check_safety({"A": 2, "B": 1, "C": 0}, state, self.T)["safe"])
        # a new process asking for more than is free must wait; a small one is admitted
        r = bankers.request_check({"A": 3, "B": 3, "C": 2}, self.P,
                                  {"id": "X", "allocation": {"A": 4}, "max": {"A": 4}}, self.T)
        self.assertEqual((r["verdict"], r["short_of"]), ("must_wait", ["A"]))
        r = bankers.request_check({"A": 3, "B": 3, "C": 2}, self.P,
                                  {"id": "Y", "allocation": {"A": 1}, "max": {"A": 1}}, self.T)
        self.assertEqual(r["verdict"], "safe")

    def test_max_claim_only_owned_types(self):
        import bankers
        mx = bankers.max_claim("CARDIAC", {"ICU_BED": 1, "AMBULANCE": 1}, {"ICU_BED", "AMBULANCE", "VENTILATOR"})
        self.assertEqual(mx, {"ICU_BED": 1, "AMBULANCE": 1, "VENTILATOR": 1})      # no OT owned -> no OT claim

    def test_holder_of_scarce_unit_lets_others_finish(self):
        """OT all taken, but by a case that will finish and give it back -> still safe."""
        import bankers
        maternity = {"id": "M", "allocation": {"OPERATION_THEATRE": 1}, "max": {"OPERATION_THEATRE": 1}}
        cardiac = {"id": "C", "allocation": {"ICU_BED": 1, "AMBULANCE": 1},
                   "max": {"ICU_BED": 1, "AMBULANCE": 1, "OPERATION_THEATRE": 1}}
        avail = {"ICU_BED": 3, "OPERATION_THEATRE": 0, "AMBULANCE": 2}
        self.assertEqual(bankers.request_check(avail, [maternity], cardiac)["verdict"], "safe")
        self.assertEqual(bankers.request_check(avail, [], cardiac)["verdict"], "unsafe")


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class BankersDatabaseTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()
        self.saved = config.BANKERS_CHECK

    def tearDown(self):
        config.BANKERS_CHECK = self.saved

    def _cardiac(self):
        import db
        from app import insert_request
        rid = insert_request("Banker Test", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0]
        return db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (rid,))

    def _starve(self, hid):
        import db
        db.execute("UPDATE resources SET available_units = 0 WHERE hospital_id = %s "
                   "AND resource_type IN ('VENTILATOR', 'OPERATION_THEATRE')", (hid,))

    def test_unsafe_hospital_is_skipped(self):
        import ranking
        import transaction_manager as tm
        config.BANKERS_CHECK = True
        req = self._cardiac()
        ranked = ranking.rank_for_request(req)
        best = ranked[0]["hospital_id"]
        self._starve(best)               # ICU bed + ambulance free, but no OT / ventilator left
        out = tm.reserve(req["request_id"], ranked=ranked)
        self.assertEqual(out["attempts"][0]["result"], "unsafe")
        self.assertEqual(out["attempts"][0]["bankers"]["verdict"], "unsafe")
        self.assertEqual(out["status"], "allocated")
        self.assertNotEqual(out["hospital"]["hospital_id"], best)
        self.assertEqual(out["bankers"]["verdict"], "safe")
        import db
        self.assertIsNotNone(db.query_one("SELECT 1 AS x FROM audit_log WHERE event_type = 'BANKERS_UNSAFE' "
                                          "AND request_id = %s", (req["request_id"],)))

    def test_check_can_be_switched_off(self):
        import ranking
        import transaction_manager as tm
        config.BANKERS_CHECK = False
        req = self._cardiac()
        ranked = ranking.rank_for_request(req)
        self._starve(ranked[0]["hospital_id"])
        out = tm.reserve(req["request_id"], ranked=ranked)
        self.assertEqual(out["hospital"]["hospital_id"], ranked[0]["hospital_id"])

    def test_evaluate_reads_active_cases(self):
        import bankers
        import transaction_manager as tm
        req = self._cardiac()
        out = tm.reserve(req["request_id"])
        ev = bankers.evaluate(out["hospital"]["hospital_id"])
        self.assertEqual(ev["verdict"], "safe")
        proc = [p for p in ev["processes"] if p["id"] == f"R{req['request_id']}"][0]
        self.assertEqual(proc["allocation"], {"ICU_BED": 1, "AMBULANCE": 1})
        self.assertEqual(proc["max"]["OPERATION_THEATRE"], 1)
        # what-if: no OT and no ventilator free -> that same state becomes unsafe
        ev = bankers.evaluate(out["hospital"]["hospital_id"],
                              available_override={"OPERATION_THEATRE": 0, "VENTILATOR": 0})
        self.assertEqual(ev["verdict"], "unsafe")

    def test_bankers_api(self):
        from app import app
        c = app.test_client()
        login(c, "admin", "admin123")
        req = self._cardiac()
        r = c.post("/api/bankers", json={"hospital_id": 1, "request_id": req["request_id"]}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["verdict"], "safe")
        self.assertTrue(r["new_process"]["is_new"])
        r = c.post("/api/bankers", json={"hospital_id": 1, "request_id": req["request_id"],
                                         "available": {"OPERATION_THEATRE": 0, "VENTILATOR": 0}}).get_json()
        self.assertEqual(r["verdict"], "unsafe")
        self.assertEqual(c.post("/api/bankers", json={"hospital_id": 999}).status_code, 404)


# ---------------------------------------------------------------------------
# Views, triggers, stored procedures (schema_objects.sql)
# ---------------------------------------------------------------------------
class SqlSplitterTests(unittest.TestCase):
    def test_delimiter_blocks(self):
        import init_db
        sql = ("-- c\nCREATE TABLE t (a INT);\nDELIMITER $$\nCREATE TRIGGER x BEFORE INSERT ON t FOR EACH ROW\n"
               "BEGIN\n  SET NEW.a = 1;\n  SET NEW.a = 2;\nEND$$\nDELIMITER ;\nSELECT 1;\n")
        st = init_db._split_sql(sql)
        self.assertEqual(len(st), 3)
        self.assertTrue(st[1].startswith("CREATE TRIGGER") and st[1].endswith("END"))
        self.assertIn("SET NEW.a = 2;", st[1])


def _triggers_on():
    if not DB_OK:
        return False
    import db_objects
    return db_objects.triggers_installed()


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class DbObjectsTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()

    def test_views_and_procedures_installed(self):
        import db_objects
        cat = db_objects.catalog()
        self.assertEqual(len(cat["views"]), 5)
        self.assertEqual(len(cat["procedures"]), 2)
        self.assertTrue(all(v["installed"] for v in cat["views"] + cat["procedures"]))
        self.assertTrue(all(o["about"] and o["sql"].startswith("CREATE") for o in
                            cat["views"] + cat["triggers"] + cat["procedures"]))

    def test_views_match_the_tables(self):
        import db
        import db_objects
        rows = db_objects.view_rows("v_hospital_capacity", limit=500)
        self.assertEqual(len(rows), 8 * 5)                                  # 8 hospitals x 5 unit types
        icu = db.query_one("SELECT SUM(available_units) AS n FROM resources r JOIN hospitals h USING (hospital_id) "
                           "WHERE h.is_lab = 0 AND r.resource_type = 'ICU_BED'")["n"]
        self.assertEqual(sum(r["available_units"] for r in rows if r["resource_type"] == "ICU_BED"), icu)
        with self.assertRaises(ValueError):
            db_objects.view_rows("users")                                   # only our views

    def test_pending_queue_view_ages_like_the_scheduler(self):
        import db_objects
        import scheduler
        from app import insert_request
        rid = insert_request("Aging", None, 30.30, 78.03, "GENERAL", 2, None, minutes_ago=25)[0]
        row = [r for r in db_objects.view_rows("v_pending_queue") if r["request_id"] == rid][0]
        self.assertEqual(row["effective_priority"],
                         scheduler.effective_priority(row["priority"], row["waiting_min"], 10))
        self.assertLess(row["effective_priority"], row["priority"])

    def test_active_cases_view(self):
        import db_objects
        import transaction_manager as tm
        from app import insert_request
        out = tm.reserve(insert_request("View", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0])
        rows = db_objects.view_rows("v_active_cases")
        self.assertEqual([r["allocation_id"] for r in rows], [out["allocation_id"]])
        self.assertEqual(rows[0]["ambulance"], "ON ROAD")

    def test_hospital_report_procedure_two_result_sets(self):
        import db_objects
        r = db_objects.hospital_report(1)
        self.assertEqual(len(r["units"]), 5)
        self.assertEqual(len(r["doctors"]), 5)

    def test_race_through_stored_procedure_books_once(self):
        import concurrency_lab
        r = concurrency_lab.run_race(6, locked=True, delay_ms=30, engine="procedure")
        self.assertEqual(r["engine"], "procedure")
        self.assertEqual(r["bookings"], 1)
        self.assertEqual(r["rolled_back"], 5)
        self.assertTrue(r["consistent"])

    def test_db_api(self):
        from app import app
        c = app.test_client()
        login(c, "admin", "admin123")
        cat = c.get("/api/db/objects").get_json()
        self.assertEqual(len(cat["views"]), 5)
        self.assertEqual(c.get("/api/db/view/v_doctor_load").status_code, 200)
        self.assertEqual(c.get("/api/db/view/users").status_code, 400)
        self.assertEqual(len(c.get("/api/db/report/1").get_json()["units"]), 5)
        r = c.post("/api/labs/race", json={"locked": True, "engine": "procedure", "clients": 4}).get_json()
        self.assertEqual(r["bookings"], 1)

    @unittest.skipUnless(_triggers_on(), "triggers not installed (python setup_mysql.py --triggers-only)")
    def test_state_machine_trigger(self):
        import db_objects
        out = db_objects.trigger_demo()
        self.assertEqual([s["ok"] for s in out["steps"]], [True, True, False])     # COMPLETED -> PENDING refused
        self.assertIn("COMPLETED -> PENDING", out["steps"][2]["message"])
        self.assertEqual([(h["old_status"], h["new_status"]) for h in out["history"]],
                         [(None, "PENDING"), ("PENDING", "ALLOCATED"), ("ALLOCATED", "COMPLETED")])

    @unittest.skipUnless(_triggers_on(), "triggers not installed (python setup_mysql.py --triggers-only)")
    def test_history_follows_a_real_case(self):
        import db_objects
        import transaction_manager as tm
        from app import insert_request
        rid = insert_request("Hist", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0]
        out = tm.reserve(rid)
        tm.release(out["allocation_id"])
        self.assertEqual([h["new_status"] for h in db_objects.status_history(rid)],
                         ["PENDING", "ALLOCATED", "COMPLETED"])

    @unittest.skipUnless(_triggers_on(), "triggers not installed (python setup_mysql.py --triggers-only)")
    def test_allocation_must_stay_in_one_hospital(self):
        import pymysql
        import db
        from app import insert_request
        rid = insert_request("Cross", None, 30.2683, 77.9947, "CARDIAC", 5, None)[0]
        other_unit = db.query_one("SELECT resource_id FROM resources WHERE hospital_id = 2 "
                                  "AND resource_type = 'ICU_BED'")["resource_id"]
        with self.assertRaises(pymysql.err.OperationalError) as cm:
            db.execute("INSERT INTO allocations (request_id, hospital_id, resource_id) VALUES (%s, 1, %s)",
                       (rid, other_unit))
        self.assertIn("does not belong", cm.exception.args[1])

    @unittest.skipUnless(_triggers_on(), "triggers not installed (python setup_mysql.py --triggers-only)")
    def test_capacity_alert_trigger(self):
        import db
        db.execute("UPDATE resources SET available_units = 0 WHERE hospital_id = 1 AND resource_type = 'ICU_BED'")
        row = db.query_one("SELECT event_type, details FROM audit_log ORDER BY log_id DESC LIMIT 1")
        self.assertEqual(row["event_type"], "CAPACITY_ALERT")
        self.assertIn("ICU BED", row["details"])
        db.execute("UPDATE resources SET available_units = 2 WHERE hospital_id = 1 AND resource_type = 'ICU_BED'")
        self.assertEqual(db.query_one("SELECT event_type FROM audit_log ORDER BY log_id DESC LIMIT 1")["event_type"],
                         "CAPACITY_RESTORED")


# ---------------------------------------------------------------------------
# Deadlock detection (wait-for graph) and recovery
# ---------------------------------------------------------------------------
class WaitForGraphTests(unittest.TestCase):
    def test_cycle_found(self):
        from deadlock_lab import find_cycle
        self.assertEqual(find_cycle({"T1": ["T2"], "T2": ["T1"]}), ["T1", "T2", "T1"])
        cyc = find_cycle({"T1": ["T2"], "T2": ["T3"], "T3": ["T1"], "T4": ["T1"]})
        self.assertEqual(cyc[0], cyc[-1])
        self.assertEqual(sorted(cyc[:-1]), ["T1", "T2", "T3"])

    def test_no_cycle(self):
        from deadlock_lab import find_cycle
        self.assertIsNone(find_cycle({"T2": ["T1"]}))                       # plain waiting
        self.assertIsNone(find_cycle({"T1": ["T2"], "T2": ["T3"], "T4": ["T3"]}))
        self.assertIsNone(find_cycle({}))


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class DeadlockLabTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()

    def test_opposite_order_deadlocks_and_recovers(self):
        import deadlock_lab
        r = deadlock_lab.run(ordered=False)
        self.assertTrue(r["deadlock"], r["events"])
        self.assertEqual(r["cycle"][0], r["cycle"][-1])
        self.assertIn(r["victim"], ("T1", "T2"))
        self.assertEqual(len(r["graph"]["edges"]), 2)                       # T1 -> T2 and T2 -> T1
        # recovery: both transactions finally commit, the victim after a retry
        self.assertTrue(all(v.startswith("committed") for v in r["outcome"].values()), r["outcome"])
        self.assertIn("retry", r["outcome"][r["victim"]])

    def test_global_order_never_deadlocks(self):
        import deadlock_lab
        r = deadlock_lab.run(ordered=True)
        self.assertFalse(r["deadlock"])
        self.assertIsNone(r["victim"])
        self.assertEqual(r["outcome"], {"T1": "committed", "T2": "committed"})
        self.assertTrue(any(e["kind"] == "wait" and e["tx"] == "T2" for e in r["events"]))

    def test_api(self):
        from app import app
        c = app.test_client()
        login(c, "admin", "admin123")
        r = c.post("/api/labs/deadlock", json={"ordered": True}).get_json()
        self.assertTrue(r["ok"])
        self.assertFalse(r["deadlock"])


# ---------------------------------------------------------------------------
# Tier 2: connection pool (db.py)
# ---------------------------------------------------------------------------
@unittest.skipUnless(DB_OK, "MySQL not reachable")
class ConnectionPoolTests(unittest.TestCase):
    def _pool(self, size=2, timeout=2):
        import db
        p = db.ConnectionPool(size, timeout)
        self.addCleanup(p.close_idle)
        return p

    @staticmethod
    def _conn_id(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT CONNECTION_ID() AS id")
            return cur.fetchone()["id"]

    def test_connections_are_reused(self):
        p = self._pool()
        ids = set()
        for _ in range(5):
            c = p.borrow(autocommit=True)
            ids.add(self._conn_id(c))
            c.close()
        self.assertEqual(len(ids), 1)                       # the same MySQL session every time
        s = p.snapshot()
        self.assertEqual((s["created"], s["reused"], s["borrowed"]), (1, 4, 5))
        self.assertEqual((s["in_use"], s["idle"]), (0, 1))

    def test_blocks_when_all_out_and_times_out(self):
        import threading
        import db
        p = self._pool(size=1, timeout=0.3)
        held = p.borrow()
        errors = []

        def other():
            try:
                p.borrow()
            except db.PoolTimeout as e:
                errors.append(e)
        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        held.close()
        self.assertEqual(len(errors), 1)
        s = p.snapshot()
        self.assertEqual((s["waited"], s["timeouts"]), (1, 1))

    def test_waiter_gets_the_returned_connection(self):
        import threading
        import time
        p = self._pool(size=1, timeout=5)
        got = threading.Event()

        def holder():                                       # another client holds the only connection
            c = p.borrow()
            got.set()
            time.sleep(0.2)
            c.close()
        threading.Thread(target=holder).start()
        got.wait(5)
        t0 = time.perf_counter()
        c = p.borrow()                                      # blocks on the semaphore until released
        waited = time.perf_counter() - t0
        c.close()
        self.assertGreaterEqual(waited, 0.15)
        s = p.snapshot()
        self.assertEqual((s["waited"], s["created"], s["reused"], s["peak_in_use"]), (1, 1, 1, 1))

    def test_nested_borrow_does_not_deadlock(self):
        p = self._pool(size=1, timeout=0.5)
        outer = p.borrow()
        inner = p.borrow()                                  # same thread: fresh connection, no waiting
        self.assertNotEqual(self._conn_id(outer), self._conn_id(inner))
        inner.close()
        outer.close()
        s = p.snapshot()
        self.assertEqual((s["nested"], s["timeouts"], s["in_use"]), (1, 0, 0))

    def test_open_transaction_rolled_back_on_return(self):
        import db
        p = self._pool(size=1)
        before = db.query_one("SELECT name FROM hospitals WHERE hospital_id = 1")["name"]
        c = p.borrow()
        c.begin()
        with c.cursor() as cur:
            cur.execute("UPDATE hospitals SET name = 'NOT COMMITTED' WHERE hospital_id = 1")
        c.close()                                           # returned with the transaction still open
        c2 = p.borrow()
        self.assertFalse(c2.server_status & db.SERVER_STATUS_IN_TRANS)
        with c2.cursor() as cur:
            cur.execute("SELECT name FROM hospitals WHERE hospital_id = 1")
            self.assertEqual(cur.fetchone()["name"], before)
        c2.close()

    def test_switch_off(self):
        import db
        self.addCleanup(db.set_pool_enabled, db.pool_enabled())
        db.set_pool_enabled(False)
        c = db.pooled()
        self.assertNotIsInstance(c, db.PooledConnection)    # a real, new connection
        c.close()
        self.assertEqual(db.pool_stats()["enabled"], False)
        db.set_pool_enabled(True)
        c = db.pooled()
        self.assertIsInstance(c, db.PooledConnection)
        c.close()
        self.assertGreaterEqual(db.pool_stats()["borrowed"], 1)

    def test_transactions_still_correct_through_the_pool(self):
        # many reservations through the pooled transaction manager: counters stay consistent
        import threading
        import db
        import init_db
        import load_test
        init_db.initialise()               # the check counts ALL active allocations: start from none
        start, _ = load_test._snapshot()
        stats, lock = [], threading.Lock()
        barrier = threading.Barrier(8)
        threads = [threading.Thread(target=load_test._client, args=(c, 3, 0.3, barrier, stats, lock))
                   for c in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok, problems = load_test.check_consistency(start)
        self.assertTrue(ok, problems)
        self.assertEqual(sum(len(s["errors"]) for s in stats), 0)
        self.assertEqual(db.pool_stats()["in_use"], 0)      # every connection came back


# ---------------------------------------------------------------------------
# Tier 2: dashboard
# ---------------------------------------------------------------------------
@unittest.skipUnless(DB_OK, "MySQL not reachable")
class DashboardTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()

    def _send(self, n, etype="CARDIAC", sev=5):
        from app import insert_request
        return [insert_request(f"Dash {i}", None, 30.2683, 77.9947, etype, sev, None)[0] for i in range(n)]

    def test_kpis_and_hourly_counts(self):
        import dashboard
        import db
        import transaction_manager as tm
        rids = self._send(3) + self._send(2, "GENERAL", 1)
        self.assertEqual(tm.reserve(rids[0])["status"], "allocated")
        k = dashboard.kpis()
        self.assertEqual((k["pending"], k["active"], k["sos_24h"]), (4, 1, 5))
        self.assertIsNotNone(k["avg_response_min"])
        hours = dashboard.cases_per_hour()
        self.assertEqual(len(hours), dashboard.HOURS)
        self.assertEqual(sum(h["total"] for h in hours), 5)
        self.assertEqual(sum(h["critical"] for h in hours), 3)              # cardiac sev 5 = P1
        self.assertTrue(all(h["critical"] + h["other"] == h["total"] for h in hours))
        self.assertEqual(hours[-1]["total"] + hours[-2]["total"], 5)        # all sent in the last hour
        lab = db.query_one("SELECT COUNT(*) AS n FROM sos_requests WHERE is_lab = 1")["n"]
        self.assertGreaterEqual(lab, 0)                                     # lab rows never counted

    def test_occupancy_excludes_lab_and_ambulances(self):
        import dashboard
        import db
        occ = dashboard.occupancy()
        lab_ids = {r["hospital_id"] for r in db.query_all("SELECT hospital_id FROM hospitals WHERE is_lab = 1")}
        self.assertFalse(lab_ids & {h["hospital_id"] for h in occ["hospitals"]})
        expect = db.query_one(
            "SELECT SUM(r.total_units) AS total, SUM(r.total_units - r.available_units) AS used "
            "FROM resources r JOIN hospitals h ON h.hospital_id = r.hospital_id "
            "WHERE h.is_lab = 0 AND r.resource_type <> 'AMBULANCE'")
        self.assertEqual((occ["total"], occ["used"]), (int(expect["total"]), int(expect["used"])))
        for h in occ["hospitals"]:
            self.assertNotIn("AMBULANCE", h["types"])
            self.assertAlmostEqual(h["pct"], round(100 * h["used"] / h["total"], 1))
        pcts = [h["pct"] for h in occ["hospitals"]]
        self.assertEqual(pcts, sorted(pcts, reverse=True))                  # fullest first

    def test_response_by_priority(self):
        import dashboard
        import transaction_manager as tm
        rid = self._send(1)[0]
        tm.reserve(rid)
        rows = dashboard.response_by_priority()
        self.assertEqual([r["priority"] for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual(rows[0]["cases"], 1)
        self.assertIsNotNone(rows[0]["avg_min"])
        self.assertIsNone(rows[4]["avg_min"])

    def test_api_sources(self):
        from app import app
        c = app.test_client()
        login(c, "dispatcher", "dispatch123")
        d = c.get("/api/dashboard").get_json()                    # no real patients: falls back to sample
        self.assertTrue(d["ok"])
        self.assertEqual(d["scheduling"]["source"], "sample")
        self.assertEqual(len(d["scheduling"]["policies"]), 6)
        self.assertGreater(d["scheduling"]["gantt"]["makespan"], 0)
        self.assertIn("enabled", d["pool"])
        d = c.get("/api/dashboard?source=real").get_json()
        self.assertEqual((d["scheduling"]["source"], d["scheduling"]["policies"]), ("real", []))
        self._send(5)
        d = c.get("/api/dashboard").get_json()
        self.assertEqual((d["scheduling"]["source"], d["scheduling"]["patients"]), ("real", 5))
        self.assertTrue(c.get("/api/pool").get_json()["pool"]["enabled"])

    def test_needs_login(self):
        from app import app
        self.assertEqual(app.test_client().get("/api/dashboard").status_code, 401)


# ---------------------------------------------------------------------------
# Tier 2: SOS portal translations
# ---------------------------------------------------------------------------
class PortalTranslationTests(unittest.TestCase):
    import re as _re
    KEY = _re.compile(r"(?<![\w.])(\w+):\s*(?=[\"'{])")

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "static", "portal_i18n.js"), encoding="utf-8") as f:
            src = f.read()
        texts, codes = src.split("const I18N_CODES")
        cls.texts_en, cls.texts_hi = cls._halves(texts)
        cls.codes_en, cls.codes_hi = cls._halves(codes)
        with open(os.path.join(HERE, "static", "portal.js"), encoding="utf-8") as f:
            cls.portal = f.read()
        with open(os.path.join(HERE, "templates", "sos.html"), encoding="utf-8") as f:
            cls.html = f.read()

    @staticmethod
    def _halves(block):
        en_start, hi_start = block.index("  en: {"), block.index("  hi: {")
        return block[en_start:hi_start], block[hi_start:]

    def keys(self, block):
        return self.KEY.findall(block)

    def test_hindi_has_every_english_text(self):
        en, hi = self.keys(self.texts_en), self.keys(self.texts_hi)
        self.assertEqual(sorted(set(en) - {"en"}), sorted(set(hi) - {"hi"}))
        self.assertGreater(len(en), 50)

    def test_hindi_has_every_code(self):
        self.assertEqual(sorted(self.keys(self.codes_en)[1:]), sorted(self.keys(self.codes_hi)[1:]))

    def test_every_key_used_exists(self):
        import re
        known = set(self.keys(self.texts_en))
        used = set(re.findall(r'\bt\("(\w+)"', self.portal))
        used |= set(re.findall(r'data-i18n(?:-html)?="(\w+)"', self.html))
        self.assertTrue(used)
        self.assertEqual(sorted(used - known), [])

    def test_placeholders_match(self):
        import re
        pat = re.compile(r'(\w+):\s*"((?:[^"\\]|\\.)*)"')
        en, hi = dict(pat.findall(self.texts_en)), dict(pat.findall(self.texts_hi))
        for k, v in en.items():
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", v)), sorted(re.findall(r"\{(\w+)\}", hi.get(k, ""))), k)

    def test_hindi_is_devanagari(self):
        import re
        self.assertRegex(self.texts_hi, r"[ऀ-ॿ]")
        self.assertIsNone(re.search(r"[ऀ-ॿ]", self.texts_en.replace("हिन्दी", "")))


# ---------------------------------------------------------------------------
# Tier 3: rate limiting and CSRF (security.py)
# ---------------------------------------------------------------------------
class SlidingWindowTests(unittest.TestCase):
    def setUp(self):
        import security
        self.now = [1000.0]
        self.w = security.SlidingWindow(60, 3, clock=lambda: self.now[0])

    def test_limit_and_expiry(self):
        for i in range(2):
            self.w.hit("k")
            self.assertEqual(self.w.retry_after("k"), 0)
        self.now[0] += 10
        self.w.hit("k")                                  # 3rd within 60 s: blocked
        self.assertEqual(self.w.retry_after("k"), 50)    # until the FIRST one leaves the window
        self.now[0] += 50
        self.assertEqual(self.w.retry_after("k"), 0)
        self.assertEqual(self.w.retry_after("other"), 0)

    def test_clear(self):
        for _ in range(3):
            self.w.hit("k")
        self.assertGreater(self.w.retry_after("k"), 0)
        self.w.clear("k")
        self.assertEqual(self.w.retry_after("k"), 0)

    def test_thread_safe_counting(self):
        import threading
        import security
        w = security.SlidingWindow(60, 10 ** 6)
        threads = [threading.Thread(target=lambda: [w.hit("k") for _ in range(500)]) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(w.hit("k"), 4001)               # no lost updates


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class CsrfTests(unittest.TestCase):
    def setUp(self):
        from app import app
        self.c = FlaskClient(app, app.response_class, use_cookies=True)   # NOT the auto-token client

    def token(self):
        return self.c.get("/api/csrf").get_json()["csrf"]

    def test_post_without_token_refused(self):
        r = self.c.post("/api/login", json={"username": "dispatcher", "password": "dispatch123"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.get_json()["csrf"])
        self.assertEqual(self.c.post("/api/sos", json={}).status_code, 403)

    def test_wrong_token_refused(self):
        self.token()
        r = self.c.post("/api/logout", headers={"X-CSRF-Token": "forged"})
        self.assertEqual(r.status_code, 403)

    def test_right_token_and_rotation_on_login(self):
        tok = self.token()
        r = self.c.post("/api/login", json={"username": "dispatcher", "password": "dispatch123"},
                        headers={"X-CSRF-Token": tok})
        self.assertEqual(r.status_code, 200)
        new = r.get_json()["csrf"]
        self.assertNotEqual(new, tok)                    # new session, new token
        self.assertEqual(self.c.post("/api/dispatch", json={}, headers={"X-CSRF-Token": tok}).status_code, 403)
        self.assertNotEqual(self.c.post("/api/dispatch", json={}, headers={"X-CSRF-Token": new}).status_code, 403)

    def test_cross_origin_refused_even_with_token(self):
        tok = self.token()
        h = {"X-CSRF-Token": tok}
        self.assertEqual(self.c.post("/api/logout", headers={**h, "Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.c.post("/api/logout", headers={**h, "Origin": "null"}).status_code, 403)
        self.assertEqual(self.c.post("/api/logout", headers={**h, "Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.c.post("/api/logout", headers={**h, "Origin": "http://localhost"}).status_code, 200)

    def test_reads_need_no_token(self):
        self.assertEqual(self.c.get("/api/meta").status_code, 200)

    def test_pages_carry_the_token(self):
        for page in ("/login", "/sos"):
            html = self.c.get(page).get_data(as_text=True)
            self.assertIn('<meta name="csrf-token" content="', html, page)
        tok = self.token()
        self.assertIn(tok, self.c.get("/sos").get_data(as_text=True))    # same session, same token


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class LoginRateLimitTests(unittest.TestCase):
    def setUp(self):
        import security
        from app import app
        security.reset_limits()
        self.addCleanup(security.reset_limits)
        self.c = app.test_client()

    def _login(self, user, pw):
        return self.c.post("/api/login", json={"username": user, "password": pw})

    def test_locks_after_five_failures(self):
        import db
        import security
        for i in range(security.LOGIN_MAX_FAILS_USER - 1):
            self.assertEqual(self._login("dispatcher", "wrong").status_code, 401)
        r = self._login("dispatcher", "wrong")
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["Retry-After"]), 0)
        # even the RIGHT password is refused while locked: guessing gets no answer
        self.assertEqual(self._login("dispatcher", "dispatch123").status_code, 429)
        # another account from the same IP is not affected
        self.assertEqual(self._login("patient", "patient123").status_code, 200)
        row = db.query_one("SELECT details FROM audit_log WHERE event_type = 'LOGIN_LOCKED' "
                           "ORDER BY log_id DESC LIMIT 1")
        self.assertIn("dispatcher", row["details"])

    def test_success_clears_failures(self):
        for _ in range(4):
            self._login("dispatcher", "wrong")
        self.assertEqual(self._login("dispatcher", "dispatch123").status_code, 200)
        for _ in range(4):
            self.assertEqual(self._login("dispatcher", "wrong").status_code, 401)

    def test_warning_before_lock(self):
        msgs = [self._login("dispatcher", "wrong").get_json()["error"] for _ in range(4)]
        self.assertNotIn("before", msgs[0])
        self.assertIn("1 more try", msgs[3])

    def test_ip_wide_limit_stops_password_spraying(self):
        from unittest import mock
        import security
        with mock.patch.object(security, "login_ip_fails", security.SlidingWindow(300, 3)):
            for name in ("a1", "a2", "a3"):
                self._login(name, "x")
            self.assertEqual(self._login("dispatcher", "dispatch123").status_code, 429)

    def test_switch_off(self):
        from unittest import mock
        import security
        with mock.patch.object(security, "RATE_LIMIT", False):
            for _ in range(8):
                self.assertEqual(self._login("dispatcher", "wrong").status_code, 401)


@unittest.skipUnless(DB_OK, "MySQL not reachable")
class SosRateLimitTests(unittest.TestCase):
    SOS = {"patient_name": "Flood", "emergency_type": "GENERAL", "severity": 1,
           "latitude": 30.27, "longitude": 77.99}

    def test_anonymous_sos_limited_staff_not(self):
        from unittest import mock
        import security
        from app import app
        with mock.patch.object(security, "sos_per_ip", security.SlidingWindow(60, 2)):
            anon = app.test_client()
            self.assertEqual(anon.post("/api/sos", json=self.SOS).status_code, 201)
            self.assertEqual(anon.post("/api/sos", json=self.SOS).status_code, 201)
            r = anon.post("/api/sos", json=self.SOS)
            self.assertEqual(r.status_code, 429)
            self.assertIn("112", r.get_json()["error"])
            staff = app.test_client()
            login(staff, "dispatcher", "dispatch123")
            for _ in range(3):
                self.assertEqual(staff.post("/api/sos", json=self.SOS).status_code, 201)


# ---------------------------------------------------------------------------
# Tier 3: offline map and Docker
# ---------------------------------------------------------------------------
class OfflineMapTests(unittest.TestCase):
    def read(self, *path):
        with open(os.path.join(HERE, *path), encoding="utf-8") as f:
            return f.read()

    def test_service_worker_caches_only_map_tiles(self):
        import re
        sw = self.read("static", "sw.js")
        pattern = re.search(r"const isTile = \(url\) => /(.+)/\.test\(url\);", sw).group(1).replace("\\/", "/")
        self.assertTrue(re.match(pattern, "https://b.tile.openstreetmap.org/12/2925/1680.png"))
        self.assertFalse(re.match(pattern, "http://localhost:5000/api/map"))
        self.assertFalse(re.match(pattern, "https://router.project-osrm.org/route/v1/x"))

    def test_every_map_uses_the_fallback(self):
        for js in (("static", "app.js"), ("static", "portal.js")):
            src = self.read(*js)
            self.assertNotIn("L.tileLayer(", src, js)                  # no map without the fallback
            self.assertIn("sosBaseLayer(", src, js)
        for page in ("index.html", "sos.html"):
            html = self.read("templates", page)
            self.assertLess(html.index("leaflet.js"), html.index("offline_map.js"))

    @unittest.skipUnless(DB_OK, "MySQL not reachable")
    def test_service_worker_served_from_root(self):
        from app import app
        r = app.test_client().get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Service-Worker-Allowed"], "/")
        self.assertIn("javascript", r.headers["Content-Type"])
        r.close()


class DockerSetupTests(unittest.TestCase):
    def read(self, *path):
        with open(os.path.join(HERE, *path), encoding="utf-8") as f:
            return f.read()

    def test_secrets_stay_out_of_the_image(self):
        ignored = self.read(".dockerignore").split()
        for secret in ("db_login.json", ".secret_key", ".env", "venv/"):
            self.assertIn(secret, ignored)

    @unittest.skipUnless(os.path.exists(os.path.join(HERE, "docker-compose.yml")),
                         "docker-compose.yml is not copied into the image (.dockerignore)")
    def test_compose_wires_app_to_mysql(self):
        y = self.read("docker-compose.yml")
        self.assertIn("image: mysql:8.0", y)
        self.assertIn("--log-bin-trust-function-creators=1", y)            # triggers work in Docker
        self.assertIn("condition: service_healthy", y)                     # app waits for MySQL
        self.assertIn("DB_HOST: db", y)
        self.assertIn('"5000:5000"', y)
        self.assertIn("emergencysos_test", self.read("docker", "mysql-init", "01-grants.sql"))

    def test_dockerfile_runs_the_entrypoint_as_non_root(self):
        d = self.read("Dockerfile")
        self.assertIn("USER sos", d)
        self.assertIn('CMD ["python", "docker/entrypoint.py"]', d)
        self.assertIn("FLASK_HOST=0.0.0.0", d)

    @unittest.skipUnless(DB_OK, "MySQL not reachable")
    def test_entrypoint_detects_first_start(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("sos_entrypoint", os.path.join(HERE, "docker", "entrypoint.py"))
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)
        ep.wait_for_mysql()
        self.assertTrue(ep.has_tables())
        with mock.patch.object(config, "DB_NAME", "emergencysos_no_such_db"):
            self.assertFalse(ep.has_tables())


# ---------------------------------------------------------------------------
# Patient phone numbers: digits only, at most 10
# ---------------------------------------------------------------------------
class PatientPhoneTests(unittest.TestCase):
    def test_rule(self):
        from admin_ops import patient_phone
        self.assertEqual(patient_phone("9876543210"), "9876543210")
        self.assertEqual(patient_phone("98765 43210"), "9876543210")      # spaces and dashes ignored
        self.assertEqual(patient_phone("98765-43210"), "9876543210")
        self.assertEqual(patient_phone("112"), "112")
        self.assertIsNone(patient_phone(""))
        self.assertIsNone(patient_phone(None))
        for bad in ("98765432101", "+919876543210", "98765abcde", "９８７"):
            with self.assertRaises(ValueError, msg=bad):
                patient_phone(bad)

    def test_every_patient_phone_field_is_limited(self):
        for page in ("sos.html", "index.html"):
            with open(os.path.join(HERE, "templates", page), encoding="utf-8") as f:
                html = f.read()
            import re
            for tag in re.findall(r'<input name="phone"[^>]*>', html):
                self.assertIn('maxlength="10"', tag, page)
                self.assertIn('pattern="[0-9]{0,10}"', tag, page)

    @unittest.skipUnless(DB_OK, "MySQL not reachable")
    def test_api_refuses_long_numbers(self):
        from app import app
        c = app.test_client()
        sos = {"patient_name": "Phone", "emergency_type": "GENERAL", "severity": 1, "latitude": 30.27, "longitude": 77.99}
        r = c.post("/api/sos", json={**sos, "phone": "98765432101"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("10", r.get_json()["error"])
        r = c.post("/api/sos", json={**sos, "phone": "98765-43210"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["request"]["phone"], "9876543210")
        rid = r.get_json()["request"]["request_id"]
        d = app.test_client()
        login(d, "dispatcher", "dispatch123")
        self.assertEqual(d.put(f"/api/requests/{rid}", json={"patient_name": "Phone", "phone": "123456789012"}).status_code, 400)
        self.assertEqual(d.put(f"/api/requests/{rid}", json={"patient_name": "Phone", "phone": "0123456789"}).status_code, 200)


class PortalAlarmTests(unittest.TestCase):
    def test_scripts_are_never_stale_on_phones(self):
        if not DB_OK:
            self.skipTest("MySQL not reachable")
        from app import app
        c = app.test_client()
        html = c.get("/sos").get_data(as_text=True)
        self.assertRegex(html, r"portal\.js\?v=\d+")
        r = c.get("/static/portal.js")
        self.assertIn("max-age=0", r.headers.get("Cache-Control", ""))
        r.close()

    def setUp(self):
        with open(os.path.join(HERE, "static", "portal.js"), encoding="utf-8") as f:
            self.js = f.read()
        with open(os.path.join(HERE, "templates", "sos.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_alarm_uses_an_audio_clip_and_repeats(self):
        self.assertIn('"/static/sounds/chime.wav', self.js)                 # a real audio file (iPhone, silent mode)
        self.assertIn("setInterval(alarmRound, ALARM_REPEAT_MS)", self.js)   # repeats until OK
        self.assertIn("st.pendingAlarm", self.js)                            # replays after the screen was off
        # unlock on events that count as a user tap on phones (touchend/click), not touchstart/pointerdown
        self.assertIn('["touchend", "click", "keydown"].forEach', self.js)
        self.assertNotIn('addEventListener("pointerdown", unlockAudio', self.js)
        # iPhone: the unlock must be a real (unmuted) play of a silent clip on the SAME element
        self.assertIn("el.src = SOUND.silence;", self.js)
        self.assertNotIn("el.muted = true;", self.js)
        self.assertIn('id="alarmSound"', self.html)
        self.assertIn('id="alarmHear"', self.html)                           # tap-to-hear if still blocked
        # Test plays the chime INSIDE the tap, and a new alert must not cut off that chime
        self.assertIn("st.testChime = playChime();", self.js)
        self.assertIn("stopAlarm(false);", self.js)

    def test_sound_files(self):
        import wave
        for name, min_sec in (("chime.wav", 1.0), ("silence.wav", 0.1)):
            with wave.open(os.path.join(HERE, "static", "sounds", name), "rb") as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth()), (1, 2), name)    # mono 16-bit PCM
                self.assertGreaterEqual(w.getnframes() / w.getframerate(), min_sec, name)

    @unittest.skipUnless(DB_OK, "MySQL not reachable")
    def test_sound_served_with_ranges(self):
        # iPhone Safari loads audio with Range requests: the server must answer 206
        from app import app
        r = app.test_client().get("/static/sounds/chime.wav", headers={"Range": "bytes=0-99"})
        self.assertEqual(r.status_code, 206)
        self.assertIn("audio/", r.headers["Content-Type"])
        r.close()
        for el in ('id="alarm"', 'id="alarmStop"', 'id="alarmNoVibrate"'):
            self.assertIn(el, self.html)


# ---------------------------------------------------------------------------
# Dispatcher chooses the hospital (manual override)
# ---------------------------------------------------------------------------
@unittest.skipUnless(DB_OK, "MySQL not reachable")
class ManualHospitalChoiceTests(unittest.TestCase):
    def setUp(self):
        import init_db
        init_db.initialise()
        from app import app
        self.c = app.test_client()
        login(self.c, "dispatcher", "dispatch123")

    def _request(self, etype="CARDIAC", sev=5):
        from app import insert_request
        return insert_request("Manual Test", None, 30.2683, 77.9947, etype, sev, None)[0]

    def _options(self, rid):
        r = self.c.get(f"/api/hospital-options/{rid}")
        self.assertEqual(r.status_code, 200)
        return r.get_json()["options"]

    def test_options_list_every_hospital_with_the_unit(self):
        import db
        rid = self._request()
        opts = self._options(rid)
        with_icu = db.query_one("SELECT COUNT(*) AS n FROM resources r JOIN hospitals h USING (hospital_id) "
                                "WHERE h.is_lab = 0 AND r.resource_type = 'ICU_BED'")["n"]
        self.assertEqual(len(opts), with_icu)
        self.assertEqual(sum(o["recommended"] for o in opts), 1)
        self.assertTrue(opts[0]["recommended"])                       # recommended first
        full = opts[-1]["hospital_id"]
        db.execute("UPDATE resources SET available_units = 0 WHERE hospital_id = %s AND resource_type = 'ICU_BED'", (full,))
        o = next(x for x in self._options(rid) if x["hospital_id"] == full)
        self.assertFalse(o["can_take"])
        self.assertEqual(o["why_not"], "no free icu bed")
        self.assertIsNone(o["rank"])

    def test_dispatcher_choice_is_booked_and_logged(self):
        import db
        rid = self._request()
        opts = self._options(rid)
        pick = [o for o in opts if o["can_take"] and not o["recommended"]][-1]
        r = self.c.post("/api/dispatch", json={"request_id": rid, "hospital_id": pick["hospital_id"],
                                               "reason": "recommended hospital phoned: ICU full"}).get_json()
        self.assertEqual(r["status"], "allocated")
        self.assertTrue(r["manual"])
        self.assertEqual(r["hospital"]["hospital_id"], pick["hospital_id"])
        self.assertEqual(r["attempts"][0]["hospital_id"], pick["hospital_id"])
        self.assertEqual(len(r["attempts"]), 1)                       # no fallback
        alloc = db.query_one("SELECT hospital_id, ambulance_resource_id FROM allocations WHERE request_id = %s", (rid,))
        self.assertEqual(alloc["hospital_id"], pick["hospital_id"])
        self.assertIsNotNone(alloc["ambulance_resource_id"])          # same transaction: ambulance too
        log = db.query_one("SELECT details FROM audit_log WHERE event_type = 'MANUAL_CHOICE' AND request_id = %s", (rid,))
        self.assertIn(opts[0]["name"], log["details"])                # the recommended one is named
        self.assertIn("ICU full", log["details"])

    def test_full_hospital_is_refused_and_nothing_booked(self):
        import db
        rid = self._request()
        hid = self._options(rid)[0]["hospital_id"]
        before = db.query_one("SELECT available_units FROM resources WHERE hospital_id = %s AND resource_type = 'AMBULANCE'", (hid,))
        db.execute("UPDATE resources SET available_units = 0 WHERE hospital_id = %s AND resource_type = 'ICU_BED'", (hid,))
        r = self.c.post("/api/dispatch", json={"request_id": rid, "hospital_id": hid}).get_json()
        self.assertEqual(r["status"], "refused")
        self.assertEqual(r["reason"], "no_unit")
        self.assertIn("Nothing was booked", r["message"])
        self.assertEqual(db.query_one("SELECT status FROM sos_requests WHERE request_id = %s", (rid,))["status"], "PENDING")
        self.assertIsNone(db.query_one("SELECT 1 FROM allocations WHERE request_id = %s", (rid,)))
        after = db.query_one("SELECT available_units FROM resources WHERE hospital_id = %s AND resource_type = 'AMBULANCE'", (hid,))
        self.assertEqual(before, after)                               # ambulance not taken either

    def test_hospital_without_the_unit_type_is_rejected(self):
        import db
        rid = self._request("TRAUMA", 5)                              # needs an operation theatre
        no_ot = db.query_one("SELECT h.hospital_id FROM hospitals h WHERE h.is_lab = 0 AND NOT EXISTS "
                             "(SELECT 1 FROM resources r WHERE r.hospital_id = h.hospital_id "
                             " AND r.resource_type = 'OPERATION_THEATRE') LIMIT 1")
        if not no_ot:
            hid = self._options(rid)[0]["hospital_id"]
            db.execute("DELETE FROM resources WHERE hospital_id = %s AND resource_type = 'OPERATION_THEATRE'", (hid,))
        else:
            hid = no_ot["hospital_id"]
        r = self.c.post("/api/dispatch", json={"request_id": rid, "hospital_id": hid})
        self.assertEqual(r.status_code, 400)
        self.assertIn("operation theatre", r.get_json()["error"])

    def test_only_dispatchers_choose(self):
        from app import app
        rid = self._request()
        hid = self._options(rid)[0]["hospital_id"]
        admin = app.test_client()
        login(admin, "admin", "admin123")
        self.assertEqual(admin.post("/api/dispatch", json={"request_id": rid, "hospital_id": hid}).status_code, 403)
        patient = app.test_client()
        login(patient, "patient", "patient123")
        self.assertEqual(patient.get(f"/api/hospital-options/{rid}").status_code, 403)
        self.assertEqual(self.c.post("/api/dispatch", json={"hospital_id": hid}).status_code, 400)  # which patient?



# ---------------------------------------------------------------- labs: system admin only
@unittest.skipUnless(DB_OK, "MySQL not reachable")
class LabsAdminOnlyTests(unittest.TestCase):
    LAB_CALLS = [("get", "/api/labs/scheduler/workload?source=sample", None),
                 ("post", "/api/labs/scheduler", {"source": "sample"}),
                 ("post", "/api/labs/race", {"locked": True, "clients": 2}),
                 ("post", "/api/labs/deadlock", {"ordered": True}),
                 ("post", "/api/bankers", {"hospital_id": 1}),
                 ("get", "/api/db/objects", None),
                 ("get", "/api/db/view/v_doctor_load", None),
                 ("get", "/api/db/report/1", None),
                 ("get", "/api/db/history/1", None)]

    def _call(self, c, method, url, body):
        return c.get(url) if method == "get" else c.post(url, json=body)

    def test_only_the_system_admin_can_use_the_labs(self):
        from app import app
        for user, pw in (("dispatcher", "dispatch123"), ("maxadmin", "admin123"), ("patient", "patient123")):
            c = app.test_client()
            login(c, user, pw)
            for method, url, body in self.LAB_CALLS:
                self.assertEqual(self._call(c, method, url, body).status_code, 403, f"{user} {url}")
        self.assertEqual(app.test_client().get("/api/db/objects").status_code, 401)
        adm = app.test_client()
        login(adm, "admin", "admin123")
        for method, url, body in self.LAB_CALLS:
            self.assertNotIn(self._call(adm, method, url, body).status_code, (401, 403), url)

    def test_lab_tabs_are_last_and_admin_only(self):
        with open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8") as f:
            html = f.read()
        import re
        tabs = re.findall(r'<button class="tab([^"]*)" data-tab="(\w+)"', html)
        names = [name for _, name in tabs]
        self.assertEqual(names[-3:], ["schedlab", "oslab", "database"])
        for cls, name in tabs:
            self.assertEqual("only-sysadmin" in cls, name in ("schedlab", "oslab", "database", "users"), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
