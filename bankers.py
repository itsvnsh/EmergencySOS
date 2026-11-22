"""
bankers.py - Banker's algorithm (deadlock AVOIDANCE) for hospital admissions.

OS mapping, one hospital at a time:
  resource types  R = ICU_BED, VENTILATOR, OPERATION_THEATRE, GENERAL_BED, AMBULANCE
  processes       P = the ACTIVE cases at that hospital
  Available[R]    = free units now (resources.available_units)
  Allocation[P,R] = what each case holds: 1 unit of its care type, plus the
                    ambulance while it is still on its round trip
  Max[P,R]        = the most each case may ever hold. A patient can get worse:
                    a cardiac patient may still need a ventilator and an
                    operation theatre (ESCALATION below). Max = Allocation plus
                    those claims, for the unit types the hospital owns.
  Need[P,R]       = Max - Allocation

Safety algorithm (textbook):
  Work = Available, Finish[P] = false
  repeat: find P with Finish[P] = false and Need[P] <= Work
          Work = Work + Allocation[P], Finish[P] = true   (P can finish, frees what it holds)
  safe  <=>  every Finish[P] is true. The order found is a SAFE SEQUENCE.

Resource-request algorithm, used before every admission:
  pretend to admit the new patient (Available -= request, add the process),
  run the safety algorithm, and admit ONLY if the state is still safe.
  Otherwise the transaction rolls back and the next ranked hospital is tried.

In plain words: a hospital only takes a new patient if it could still treat
every patient it already has in their worst case, one after another. A safe
state can never deadlock; an unsafe one might.
"""
import config
import db

RESOURCE_ORDER = ["ICU_BED", "VENTILATOR", "OPERATION_THEATRE", "GENERAL_BED", "AMBULANCE"]
SHORT = {"ICU_BED": "ICU", "VENTILATOR": "VENT", "OPERATION_THEATRE": "OT",
         "GENERAL_BED": "GEN", "AMBULANCE": "AMB"}

# extra units a case may still claim during treatment (its worst case)
ESCALATION = {
    "CARDIAC":     {"VENTILATOR": 1, "OPERATION_THEATRE": 1},   # arrest, bypass surgery
    "STROKE":      {"VENTILATOR": 1, "OPERATION_THEATRE": 1},   # airway support, thrombectomy
    "TRAUMA":      {"OPERATION_THEATRE": 1, "ICU_BED": 1},      # surgery, then ICU
    "RESPIRATORY": {"VENTILATOR": 1},
    "BURN":        {"OPERATION_THEATRE": 1},                    # grafting
    "MATERNITY":   {"OPERATION_THEATRE": 1},                    # caesarean
    "POISONING":   {"ICU_BED": 1, "VENTILATOR": 1},
    "GENERAL":     {},
}


# ---------------------------------------------------------------------------
# Pure algorithm (no database) - unit tested in test_core.py
# ---------------------------------------------------------------------------
def _vec(d, types):
    return {t: int(d.get(t, 0) or 0) for t in types}


def check_safety(available, processes, types=None):
    """
    available : {type: units}
    processes : [{"id", "label", "allocation": {type: n}, "max": {type: n}}]
    Returns {"safe", "sequence": [ids], "blocked": [ids], "need": {id: vec},
             "steps": [{"process", "label", "need", "work_before", "work_after"}]}
    """
    types = types or RESOURCE_ORDER
    work = _vec(available, types)
    alloc = {p["id"]: _vec(p["allocation"], types) for p in processes}
    need = {p["id"]: {t: max(0, int(p["max"].get(t, 0) or 0) - alloc[p["id"]][t]) for t in types}
            for p in processes}
    finished, sequence, steps = set(), [], []
    progress = True
    while progress:
        progress = False
        for p in processes:
            pid = p["id"]
            if pid in finished or any(need[pid][t] > work[t] for t in types):
                continue
            before = dict(work)
            for t in types:
                work[t] += alloc[pid][t]
            finished.add(pid)
            sequence.append(pid)
            steps.append({"process": pid, "label": p.get("label", str(pid)), "need": need[pid],
                          "work_before": before, "work_after": dict(work)})
            progress = True
    blocked = [p["id"] for p in processes if p["id"] not in finished]
    return {"safe": not blocked, "sequence": sequence, "blocked": blocked, "need": need, "steps": steps}


def request_check(available, processes, new_process, types=None):
    """
    Banker's resource-request algorithm for a NEW process asking for new_process["allocation"].
    Returns check_safety's result plus "verdict":
      "safe"       admit: a safe sequence exists afterwards
      "unsafe"     refuse: admitting could lead to deadlock
      "must_wait"  refuse: not enough free units right now (Request > Available)
    """
    types = types or RESOURCE_ORDER
    request = _vec(new_process["allocation"], types)
    avail = _vec(available, types)
    if any(request[t] > avail[t] for t in types):
        short = [t for t in types if request[t] > avail[t]]
        return {"verdict": "must_wait", "safe": False, "sequence": [], "blocked": [new_process["id"]],
                "need": {}, "steps": [], "short_of": short}
    after = {t: avail[t] - request[t] for t in types}
    result = check_safety(after, list(processes) + [new_process], types)
    result["verdict"] = "safe" if result["safe"] else "unsafe"
    result["available_after"] = after
    return result


def max_claim(emergency_type, holding, owned_types):
    """Max[P] = what it holds now, raised to its escalation claims (only types the hospital owns)."""
    mx = dict(holding)
    for t, n in ESCALATION.get(emergency_type, {}).items():
        if t in owned_types:
            mx[t] = max(mx.get(t, 0), n)
    return mx


# ---------------------------------------------------------------------------
# Database side
# ---------------------------------------------------------------------------
def _rows(cur, sql, params):
    if cur is None:
        return db.query_all(sql, params)
    cur.execute(sql, params)
    return cur.fetchall()


def hospital_state(hospital_id, cur=None):
    """Available, totals and the active cases (as Banker's processes) of one hospital.
    Pass `cur` to read inside an open transaction (the reservation does)."""
    res = _rows(cur, "SELECT resource_type, total_units, available_units FROM resources "
                     "WHERE hospital_id = %s", (hospital_id,))
    total = {r["resource_type"]: int(r["total_units"]) for r in res}
    available = {r["resource_type"]: int(r["available_units"]) for r in res}
    owned = {t for t, n in total.items() if n > 0}
    cases = _rows(cur, "SELECT a.allocation_id, a.request_id, r.patient_name, r.emergency_type, "
                       "r.required_resource, (a.ambulance_resource_id IS NOT NULL "
                       "AND a.ambulance_returned_at IS NULL) AS amb_out "
                       "FROM allocations a JOIN sos_requests r ON r.request_id = a.request_id "
                       "WHERE a.hospital_id = %s AND a.status = 'ACTIVE' ORDER BY a.allocation_id",
                  (hospital_id,))
    processes = []
    for c in cases:
        holding = {c["required_resource"]: 1}
        if c["amb_out"]:
            holding["AMBULANCE"] = 1
        processes.append({"id": f"R{c['request_id']}", "label": f"#{c['request_id']} {c['patient_name']}",
                          "emergency_type": c["emergency_type"], "allocation": holding,
                          "max": max_claim(c["emergency_type"], holding, owned)})
    return {"hospital_id": hospital_id, "total": total, "available": available,
            "owned": sorted(owned, key=RESOURCE_ORDER.index), "processes": processes}


def new_process(request, owned):
    """The patient asking to be admitted: one unit of its care type + an ambulance."""
    holding = {request["required_resource"]: 1, "AMBULANCE": 1}
    return {"id": f"R{request['request_id']}", "label": f"NEW #{request['request_id']} "
                                                       f"{request.get('patient_name', '')}".strip(),
            "emergency_type": request["emergency_type"], "allocation": holding, "is_new": True,
            "max": max_claim(request["emergency_type"], holding, owned)}


def evaluate(hospital_id, request=None, cur=None, available_override=None):
    """
    Banker's state of one hospital. With `request` (an sos_requests row), also the
    verdict for admitting it. `available_override` lets the lab ask "what if only
    N units were free?" without touching the database.
    """
    st = hospital_state(hospital_id, cur)
    if available_override:
        for t, n in available_override.items():
            if t in st["available"]:
                st["available"][t] = max(0, int(n))
    types = [t for t in RESOURCE_ORDER if t in st["total"]]
    if request is None:
        result = check_safety(st["available"], st["processes"], types)
        result["verdict"] = "safe" if result["safe"] else "unsafe"
        newp = None
    else:
        newp = new_process(request, set(st["owned"]))
        result = request_check(st["available"], st["processes"], newp, types)
    labels = {p["id"]: p["label"] for p in st["processes"] + ([newp] if newp else [])}
    return {**st, "types": types, "new_process": newp, **result,
            "sequence_labels": [labels[i] for i in result["sequence"]],
            "blocked_labels": [labels[i] for i in result["blocked"]]}


def summary(ev):
    """One line for the audit log / dispatch result."""
    if ev["verdict"] == "safe":
        return "SAFE, sequence: " + " -> ".join(ev["sequence_labels"]) if ev["sequence_labels"] else "SAFE"
    if ev["verdict"] == "must_wait":
        return "WAIT: not enough free " + ", ".join(SHORT[t] for t in ev.get("short_of", []))
    return "UNSAFE: no safe sequence for " + ", ".join(ev["blocked_labels"])


def enabled():
    return config.BANKERS_CHECK
