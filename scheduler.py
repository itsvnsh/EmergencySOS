"""
scheduler.py - OS scheduling engine for SOS requests.

SOS requests are treated as PROCESSES and the dispatcher is the single CPU:
it handles one case at a time (non-preemptive).

  AUTO      Automatic mode, used by the live dispatcher. Nobody picks an
            algorithm: every waiting patient is placed in a queue by how
            serious they are (after aging), and each queue uses the
            algorithm that suits it:
              Critical (P1-P2) -> Priority with aging  (most serious first)
              Urgent   (P3)    -> Shortest Job First   (clear quick cases fast)
              Routine  (P4-P5) -> First Come First Served (fair for minor cases)
            Critical is always served before Urgent, Urgent before Routine.
            Aging moves a long-waiting patient up into a higher queue, so a
            routine case cannot starve.

The other policies are kept for comparison in the Scheduler Lab:

  PRIORITY  Priority scheduling WITH AGING (primary policy).
            effective_priority = priority - floor(wait / AGING_INTERVAL), min 1
            -> a long-waiting low-priority request slowly moves up, so it
               cannot starve.
  FCFS      First Come First Served (by arrival time).
  SJF       Shortest Job First, non-preemptive. Burst time = est_handling_min.
  MLQ       Multilevel Queue with fixed priority between queues:
              Q1 Critical (P1-P2) ordered by priority, then arrival
              Q2 Urgent   (P3)    FCFS
              Q3 Routine  (P4-P5) SJF
            A lower queue runs only when every higher queue is empty.

Two uses:
  order_queue()  orders the LIVE pending queue (used by the dispatcher/API)
  simulate()     runs a workload to completion and reports waiting time,
                 turnaround time and the execution order (Gantt chart)
"""
import math
from datetime import datetime

import config

POLICIES = ["AUTO", "PRIORITY", "FCFS", "SJF", "MLQ"]
POLICY_NAMES = {
    "AUTO": "Automatic (smart queues)",
    "PRIORITY": "Priority with aging",
    "PRIORITY_NO_AGING": "Priority (no aging)",
    "FCFS": "First Come First Served",
    "SJF": "Shortest Job First",
    "MLQ": "Multilevel Queue",
}
CRITICAL_MAX_PRIORITY = 2      # P1 and P2 count as "critical patients" in the metrics

# Automatic mode: queue level -> (queue name, algorithm used inside that queue)
AUTO_QUEUES = {
    1: ("Critical", "Priority with aging"),
    2: ("Urgent", "Shortest Job First"),
    3: ("Routine", "First Come First Served"),
}


def effective_priority(priority, wait_min, aging_interval=None):
    """Aging: every `aging_interval` minutes of waiting improves priority by one level."""
    if aging_interval is None:
        aging_interval = config.AGING_INTERVAL_MIN
    if not aging_interval or aging_interval <= 0:
        return priority
    return max(1, priority - int(wait_min // aging_interval))


def mlq_level(priority):
    if priority <= 2:
        return 1
    if priority == 3:
        return 2
    return 3


def _sort_key(job, policy, now, aging_interval):
    """Smaller key = served first. `now` and job['arrival'] are in minutes."""
    wait = now - job["arrival"]
    tie = (job["arrival"], job["id"])
    if policy == "FCFS":
        return tie
    if policy == "SJF":
        return (job["burst"],) + tie
    if policy == "PRIORITY":
        # ties on effective priority go to the request that was originally more
        # urgent, so an aged minor case never jumps ahead of a genuine P1 case
        return (effective_priority(job["priority"], wait, aging_interval), job["priority"]) + tie
    if policy == "PRIORITY_NO_AGING":
        return (job["priority"],) + tie
    if policy == "AUTO":
        eff = effective_priority(job["priority"], wait, aging_interval)
        level = mlq_level(eff)                      # queue chosen AFTER aging
        if level == 1:
            return (1, eff, job["priority"]) + tie  # Priority with aging
        if level == 2:
            return (2, job["burst"], 0) + tie       # Shortest Job First
        return (3, 0, 0) + tie                      # First Come First Served
    if policy == "MLQ":
        level = mlq_level(job["priority"])
        if level == 1:
            return (1, job["priority"]) + tie
        if level == 2:
            return (2, 0) + tie
        return (3, job["burst"]) + tie
    raise ValueError(f"Unknown policy '{policy}'. Choose one of {POLICIES}")


# ---------------------------------------------------------------------------
# Live queue ordering
# ---------------------------------------------------------------------------
def auto_decision(priority, eff_priority, wait_min, burst):
    """Which queue/algorithm Automatic mode uses for one patient, and why (shown in the UI)."""
    level = mlq_level(eff_priority)
    queue_name, algorithm = AUTO_QUEUES[level]
    if eff_priority < priority:
        reason = (f"Waited {wait_min:g} min, so aging raised it from P{priority} to P{eff_priority} "
                  f"and moved it into the {queue_name} queue.")
    elif level == 1:
        reason = f"P{priority} is life-threatening: the most serious patient is served first."
    elif level == 2:
        reason = f"P{priority} is urgent: among urgent cases the quickest one ({burst} min) goes first."
    else:
        reason = f"P{priority} is a minor case: minor cases are served in order of arrival."
    return {"level": level, "queue": queue_name, "algorithm": algorithm, "reason": reason}


def order_queue(requests, policy="AUTO", now=None, aging_interval=None):
    """
    Order pending SOS requests (dicts from the sos_requests table) by policy.
    Returns new dicts with wait_min, effective_priority, mlq_level and position.
    """
    policy = policy.upper()
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy '{policy}'. Choose one of {POLICIES}")
    now = now or datetime.now()
    epoch = min((r["created_at"] for r in requests), default=now)

    jobs = []
    for r in requests:
        jobs.append({
            "id": r["request_id"],
            "arrival": (r["created_at"] - epoch).total_seconds() / 60.0,
            "priority": int(r["priority"]),
            "burst": int(r["est_handling_min"]),
            "row": r,
        })
    now_min = (now - epoch).total_seconds() / 60.0
    jobs.sort(key=lambda j: _sort_key(j, policy, now_min, aging_interval))

    ordered = []
    for pos, j in enumerate(jobs, start=1):
        wait = max(0.0, now_min - j["arrival"])
        item = dict(j["row"])
        item["position"] = pos
        item["wait_min"] = round(wait, 1)
        item["effective_priority"] = effective_priority(j["priority"], wait, aging_interval)
        item["mlq_level"] = mlq_level(j["priority"])
        item["auto"] = auto_decision(j["priority"], item["effective_priority"], item["wait_min"], j["burst"])
        ordered.append(item)
    return ordered


def next_request(requests, policy="AUTO", now=None):
    """The request the dispatcher should serve next (or None if the queue is empty)."""
    ordered = order_queue(requests, policy, now)
    return ordered[0] if ordered else None


# ---------------------------------------------------------------------------
# Simulation (Scheduler Lab)
# ---------------------------------------------------------------------------
def simulate(workload, policy="PRIORITY", aging_interval=None):
    """
    workload: list of dicts with id, label, arrival (min), priority (1-5), burst (min)
    Returns per-request times, averages and a Gantt list (including idle gaps).
    Non-preemptive: once the dispatcher starts a case it finishes it.
    """
    policy = policy.upper()
    if policy not in POLICY_NAMES:
        raise ValueError(f"Unknown policy '{policy}'")
    if not workload:
        raise ValueError("Workload is empty")

    pending = sorted((dict(j) for j in workload), key=lambda j: (j["arrival"], j["id"]))
    for j in pending:
        if j["burst"] <= 0 or j["arrival"] < 0:
            raise ValueError(f"Request {j['id']}: burst must be > 0 and arrival >= 0")
    time = 0
    done, gantt = [], []

    while pending:
        ready = [j for j in pending if j["arrival"] <= time]
        if not ready:                                   # dispatcher idle until next arrival
            nxt = min(j["arrival"] for j in pending)
            gantt.append({"id": None, "label": "idle", "start": time, "end": nxt})
            time = nxt
            continue
        job = min(ready, key=lambda j: _sort_key(j, policy, time, aging_interval))
        pending.remove(job)
        start, end = time, time + job["burst"]
        gantt.append({"id": job["id"], "label": job.get("label", f"R{job['id']}"),
                      "priority": job["priority"], "start": start, "end": end})
        done.append({**job, "start": start, "finish": end,
                     "waiting": start - job["arrival"], "turnaround": end - job["arrival"]})
        time = end

    critical = [d for d in done if d["priority"] <= CRITICAL_MAX_PRIORITY]

    def avg(values):
        values = list(values)
        return round(sum(values) / len(values), 2) if values else 0.0

    return {
        "policy": policy,
        "policy_name": POLICY_NAMES[policy],
        "results": sorted(done, key=lambda d: d["id"]),
        "gantt": gantt,
        "metrics": {
            "avg_waiting": avg(d["waiting"] for d in done),
            "avg_turnaround": avg(d["turnaround"] for d in done),
            "avg_critical_waiting": avg(d["waiting"] for d in critical),
            "max_waiting": max(d["waiting"] for d in done),
            "critical_count": len(critical),
            "makespan": max(d["finish"] for d in done),
        },
    }


def compare(workload, policies=None, aging_interval=None):
    """Run several policies on the same workload (used by the Scheduler Lab)."""
    policies = policies or ["AUTO", "PRIORITY", "PRIORITY_NO_AGING", "FCFS", "SJF", "MLQ"]
    return [simulate(workload, p, aging_interval) for p in policies]


def workload_from_requests(items):
    """
    Build a simulation workload from {type, severity, arrival} items by running
    them through the classifier (so the lab uses the same rules as the live system).
    """
    from classifier import classify
    workload = []
    for i, it in enumerate(items, start=1):
        c = classify(it["type"], it["severity"])
        workload.append({
            "id": i,
            "label": it.get("label") or f"R{i}",
            "type": c["emergency_type"],
            "severity": c["severity"],
            "arrival": int(it["arrival"]),
            "priority": c["priority"],
            "burst": c["est_handling_min"],
        })
    return workload


def workload_from_db_rows(rows):
    """
    Build a simulation workload from REAL sos_requests rows (patients in the database).
    Arrival = minutes after the first of these patients sent their SOS.
    """
    if not rows:
        return []
    first = min(r["created_at"] for r in rows)
    workload = []
    for r in sorted(rows, key=lambda r: (r["created_at"], r["request_id"])):
        workload.append({
            "id": r["request_id"],
            "label": f"#{r['request_id']}",
            "patient_name": r["patient_name"],
            "status": r.get("live_status", r["status"]),
            "type": r["emergency_type"],
            "severity": int(r["severity"]),
            "arrival": round((r["created_at"] - first).total_seconds() / 60.0, 1),
            "priority": int(r["priority"]),
            "burst": int(r["est_handling_min"]),
        })
    return workload


if __name__ == "__main__":
    import json, os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_workload.json")
    with open(path, encoding="utf-8") as f:
        wl = workload_from_requests(json.load(f)["requests"])
    for res in compare(wl):
        m = res["metrics"]
        print(f"{res['policy_name']:24} avg wait {m['avg_waiting']:6.2f}  "
              f"avg TAT {m['avg_turnaround']:6.2f}  critical wait {m['avg_critical_waiting']:6.2f}  "
              f"max wait {m['max_waiting']}")
        print("   order:", " ".join(g["label"] for g in res["gantt"] if g["id"]))
