"""
load_test.py - many concurrent clients against the real MySQL database (Phase III).

Each client is a thread with its OWN database connection (its own MySQL session),
exactly like separate dispatchers / users. Every client repeats:

    send an SOS  ->  rank hospitals  ->  reserve a unit (locked transaction)
    and now and then release an active case (also a locked transaction)

so reservations and releases on the SAME hot rows (the nearest hospital's ICU
counter, the same cardiologist) collide all the time.

After the run it checks CONSISTENCY - the thing locking is for:
    for every resource:  used units  == used at start + ACTIVE allocations on it
    for every doctor:    current_cases == ACTIVE allocations with that doctor
    no request has more than one allocation, no counter went negative

It also compares isolation levels (the tuning part):
    python load_test.py                       # 20 clients x 15 ops, all isolation levels
    python load_test.py --clients 50 --ops 20
    python load_test.py --isolation "REPEATABLE READ"
    python load_test.py --http http://127.0.0.1:5000 --clients 20   # through the running web app

It uses the TEST database (<DB_NAME>_test) and reloads it, so demo data is safe.
"""
import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

if "--keep-db-name" not in sys.argv and not os.environ.get("DB_NAME", "").endswith("_test"):
    os.environ["DB_NAME"] = os.environ.get("DB_NAME", "emergencysos") + "_test"
os.environ.setdefault("ROUTING_MODE", "off")      # no network calls while hammering the DB

import config      # noqa: E402  (must come after DB_NAME is set)
import db          # noqa: E402
import init_db     # noqa: E402
import ranking     # noqa: E402
import transaction_manager as tm   # noqa: E402

ISOLATION_LEVELS = ["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]

# patient locations around Dehradun and the case mix (type, severity)
AREA = (30.18, 30.38, 77.95, 78.12)
CASES = [("CARDIAC", 5), ("CARDIAC", 4), ("STROKE", 4), ("TRAUMA", 4), ("RESPIRATORY", 3),
         ("BURN", 3), ("MATERNITY", 3), ("POISONING", 2), ("GENERAL", 2), ("GENERAL", 1)]


def _snapshot():
    res = {r["resource_id"]: r["total_units"] - r["available_units"]
           for r in db.query_all("SELECT resource_id, total_units, available_units FROM resources")}
    docs = {d["doctor_id"]: d["current_cases"] for d in db.query_all("SELECT doctor_id, current_cases FROM doctors")}
    return res, docs


def check_consistency(start_used):
    """Compare every counter with the allocation rows. Returns (ok, list of problems)."""
    problems = []
    used_now, docs_now = _snapshot()
    active_res = {r["resource_id"]: r["n"] for r in db.query_all(
        "SELECT resource_id, COUNT(*) AS n FROM allocations WHERE status = 'ACTIVE' GROUP BY resource_id")}
    active_doc = {r["doctor_id"]: r["n"] for r in db.query_all(
        "SELECT doctor_id, COUNT(*) AS n FROM allocations WHERE status = 'ACTIVE' "
        "AND doctor_id IS NOT NULL GROUP BY doctor_id")}
    for rid, used in used_now.items():
        expected = start_used[rid] + active_res.get(rid, 0)
        if used != expected:
            problems.append(f"resource #{rid}: {used} units in use, allocations say {expected}")
    for did, cases in docs_now.items():
        if cases != active_doc.get(did, 0):
            problems.append(f"doctor #{did}: current_cases {cases}, active allocations {active_doc.get(did, 0)}")
    dup = db.query_all("SELECT request_id, COUNT(*) AS n FROM allocations GROUP BY request_id HAVING n > 1")
    problems += [f"request #{d['request_id']} has {d['n']} allocations" for d in dup]
    bad_status = db.query_one(
        "SELECT COUNT(*) AS n FROM sos_requests r JOIN allocations a ON a.request_id = r.request_id "
        "WHERE (a.status = 'ACTIVE' AND r.status <> 'ALLOCATED') OR (a.status = 'RELEASED' AND r.status <> 'COMPLETED')")
    if bad_status["n"]:
        problems.append(f"{bad_status['n']} request(s) whose status does not match their allocation")
    return not problems, problems


def _percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round(p / 100 * (len(values) - 1)))))
    return values[k]


# ---------------------------------------------------------------------------
# direct mode: each client talks to MySQL itself (one connection per transaction)
# ---------------------------------------------------------------------------
def _client(cid, ops, release_prob, barrier, stats, lock):
    from app import insert_request          # same insert + classification as the API
    rnd = random.Random(cid)
    local = {"lat": [], "outcomes": {}, "retries": 0, "fallbacks": 0, "errors": [], "released": 0}
    barrier.wait()                          # start every client at the same moment
    for i in range(ops):
        etype, sev = rnd.choice(CASES)
        lat, lon = rnd.uniform(AREA[0], AREA[1]), rnd.uniform(AREA[2], AREA[3])
        t0 = time.perf_counter()
        try:
            rid, _ = insert_request(f"Load {cid}-{i}", None, lat, lon, etype, sev, "load test")
            req = db.query_one("SELECT * FROM sos_requests WHERE request_id = %s", (rid,))
            out = tm.reserve(rid, ranked=ranking.rank_for_request(req))
            local["outcomes"][out["status"]] = local["outcomes"].get(out["status"], 0) + 1
            local["fallbacks"] += out.get("fallbacks", 0) or 0
            local["retries"] += sum(max(0, (a.get("tx_attempts") or 1) - 1) for a in out.get("attempts", []))
            if rnd.random() < release_prob:
                active = db.query_all("SELECT allocation_id FROM allocations WHERE status = 'ACTIVE' "
                                      "ORDER BY RAND() LIMIT 1")
                if active and tm.release(active[0]["allocation_id"]).get("result") == "released":
                    local["released"] += 1
        except Exception as e:                       # anything unexpected is a failed operation
            local["errors"].append(f"{type(e).__name__}: {e}")
        local["lat"].append((time.perf_counter() - t0) * 1000)
    with lock:
        stats.append(local)


def run_direct(clients=20, ops=15, isolation=None, release_prob=0.4, lock_timeout=None, quiet=False):
    old_iso, old_to = config.ISOLATION_LEVEL, config.LOCK_WAIT_TIMEOUT_SEC
    config.ISOLATION_LEVEL = isolation or old_iso
    config.LOCK_WAIT_TIMEOUT_SEC = lock_timeout or old_to
    try:
        init_db.initialise()
        start_used, _ = _snapshot()
        stats, lock = [], threading.Lock()
        barrier = threading.Barrier(clients)
        threads = [threading.Thread(target=_client, args=(c, ops, release_prob, barrier, stats, lock))
                   for c in range(clients)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0
        ok, problems = check_consistency(start_used)
    finally:
        config.ISOLATION_LEVEL, config.LOCK_WAIT_TIMEOUT_SEC = old_iso, old_to

    lat = [x for s in stats for x in s["lat"]]
    outcomes = {}
    for s in stats:
        for k, v in s["outcomes"].items():
            outcomes[k] = outcomes.get(k, 0) + v
    result = {
        "mode": "direct", "isolation": isolation or old_iso, "clients": clients, "ops_per_client": ops,
        "operations": len(lat), "seconds": round(elapsed, 2),
        "throughput_ops_s": round(len(lat) / elapsed, 1) if elapsed else 0,
        "latency_ms": {"avg": round(statistics.mean(lat), 1) if lat else 0,
                       "p50": round(_percentile(lat, 50), 1), "p95": round(_percentile(lat, 95), 1),
                       "max": round(max(lat), 1) if lat else 0},
        "outcomes": outcomes,
        "fallbacks_stale_ranking": sum(s["fallbacks"] for s in stats),
        "deadlock_or_timeout_retries": sum(s["retries"] for s in stats),
        "releases": sum(s["released"] for s in stats),
        "errors": [e for s in stats for e in s["errors"]][:10],
        "error_count": sum(len(s["errors"]) for s in stats),
        "consistent": ok, "problems": problems[:10],
    }
    if not quiet:
        print_result(result)
    return result


# ---------------------------------------------------------------------------
# http mode: clients go through the running Flask app (login, JSON API)
# ---------------------------------------------------------------------------
def _http(opener, base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def run_http(base, clients=20, ops=10, user="dispatcher", password="dispatch123"):
    """SOS senders (no login) and dispatchers (logged in) hitting the web app at the same time."""
    lat, outcomes, errors, lock = [], {}, [], threading.Lock()
    barrier = threading.Barrier(clients)

    def worker(cid):
        rnd = random.Random(cid)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        code, _ = _http(opener, base, "/api/login", {"username": user, "password": password})
        if code != 200:
            with lock:
                errors.append(f"client {cid}: login failed ({code})")
            return
        barrier.wait()
        for i in range(ops):
            etype, sev = rnd.choice(CASES)
            t0 = time.perf_counter()
            c1, _ = _http(opener, base, "/api/sos", {
                "patient_name": f"HTTP {cid}-{i}", "emergency_type": etype, "severity": sev,
                "latitude": rnd.uniform(AREA[0], AREA[1]), "longitude": rnd.uniform(AREA[2], AREA[3])})
            c2, d = _http(opener, base, "/api/dispatch", {})
            ms = (time.perf_counter() - t0) * 1000
            with lock:
                lat.append(ms)
                key = d.get("status", f"http {c2}") if c1 == 201 else f"sos http {c1}"
                outcomes[key] = outcomes.get(key, 0) + 1

    threads = [threading.Thread(target=worker, args=(c,)) for c in range(clients)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    result = {"mode": "http", "base": base, "clients": clients, "operations": len(lat),
              "seconds": round(elapsed, 2), "throughput_ops_s": round(len(lat) / elapsed, 1) if elapsed else 0,
              "latency_ms": {"avg": round(statistics.mean(lat), 1) if lat else 0,
                             "p50": round(_percentile(lat, 50), 1), "p95": round(_percentile(lat, 95), 1),
                             "max": round(max(lat), 1) if lat else 0},
              "outcomes": outcomes, "errors": errors[:10], "error_count": len(errors)}
    print_result(result)
    print("  (check consistency afterwards with:  python load_test.py --check-only --keep-db-name)")
    return result


def print_result(r):
    print(f"\n=== {r['mode']} load test" + (f" · {r['isolation']}" if r.get("isolation") else "") + " ===")
    print(f"  {r['clients']} clients, {r['operations']} operations in {r['seconds']} s "
          f"-> {r['throughput_ops_s']} ops/s")
    l = r["latency_ms"]
    print(f"  latency ms: avg {l['avg']}  p50 {l['p50']}  p95 {l['p95']}  max {l['max']}")
    print(f"  outcomes: {r['outcomes']}")
    if "consistent" in r:
        print(f"  stale-ranking fallbacks: {r['fallbacks_stale_ranking']}   "
              f"deadlock/timeout retries: {r['deadlock_or_timeout_retries']}   releases: {r['releases']}")
        print(f"  CONSISTENT: {'YES' if r['consistent'] else 'NO'}" +
              ("" if r["consistent"] else "  " + "; ".join(r["problems"])))
    if r["error_count"]:
        print(f"  errors ({r['error_count']}): " + " | ".join(r["errors"][:3]))


def compare_isolation(clients=20, ops=15):
    rows = [run_direct(clients, ops, isolation=iso, quiet=True) for iso in ISOLATION_LEVELS]
    print(f"\n=== Isolation level comparison: {clients} clients x {ops} SOS each ===")
    print(f"{'Isolation':18} {'ops/s':>7} {'avg ms':>8} {'p95 ms':>8} {'fallbacks':>9} {'retries':>8} {'errors':>7}  consistent")
    for r in rows:
        print(f"{r['isolation']:18} {r['throughput_ops_s']:>7} {r['latency_ms']['avg']:>8} "
              f"{r['latency_ms']['p95']:>8} {r['fallbacks_stale_ranking']:>9} {r['deadlock_or_timeout_retries']:>8} "
              f"{r['error_count']:>7}  {'YES' if r['consistent'] else 'NO'}")
    fastest = max(rows, key=lambda r: r["throughput_ops_s"])
    all_ok = all(r["consistent"] for r in rows)
    print(f"\nFastest here: {fastest['isolation']}. "
          + ("Every level stayed consistent, because each booking re-checks the counter under "
             "SELECT ... FOR UPDATE\n(the lock gives the safety, not the isolation level)." if all_ok
             else "Some levels were NOT consistent - see the problems above."))
    print("fallbacks = hospital filled up between ranking and commit, booked the next one instead.\n"
          "retries   = deadlock / lock-wait timeout, transaction rolled back and run again.")
    return rows


def main():
    ap = argparse.ArgumentParser(description="EmergencySOS load test")
    ap.add_argument("--clients", type=int, default=20)
    ap.add_argument("--ops", type=int, default=15, help="SOS requests per client")
    ap.add_argument("--isolation", choices=ISOLATION_LEVELS, help="test only this isolation level")
    ap.add_argument("--http", metavar="URL", help="go through the running web app, e.g. http://127.0.0.1:5000")
    ap.add_argument("--check-only", action="store_true", help="only run the consistency check")
    ap.add_argument("--keep-db-name", action="store_true", help="use DB_NAME as is (not the _test database)")
    a = ap.parse_args()
    print(f"Database: {config.DB_NAME} on {config.DB_HOST}:{config.DB_PORT}")
    if a.check_only:
        used = {r["resource_id"]: 0 for r in db.query_all("SELECT resource_id FROM resources")}
        print("(baseline unknown in check-only mode: doctor counters and duplicates are checked)")
        ok, problems = check_consistency(used)
        doc_problems = [p for p in problems if not p.startswith("resource")]
        print("CONSISTENT" if not doc_problems else "\n".join(doc_problems))
        return
    if a.http:
        run_http(a.http.rstrip("/"), a.clients, a.ops)
    elif a.isolation:
        run_direct(a.clients, a.ops, isolation=a.isolation)
    else:
        compare_isolation(a.clients, a.ops)


if __name__ == "__main__":
    main()
