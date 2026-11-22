"""
deadlock_lab.py - deadlock DETECTION and RECOVERY, shown live on MySQL.

Two transactions, T1 and T2, both need the same TWO rows of the lab hospital:
    A = its ICU-bed counter      B = its ambulance counter
(exactly the two rows a real booking locks: the bed and the ambulance).

  ordered=False   T1 locks A then B,  T2 locks B then A
                  -> T1 holds A and waits for B, T2 holds B and waits for A
                  -> circular wait = DEADLOCK
  ordered=True    both lock A then B (the project's global lock order)
                  -> T2 simply waits until T1 commits; no cycle can form

Detection, two independent ways:
  1. Our detector (OS textbook): every transaction announces "I hold X" and
     "I am waiting for Y". Each time someone starts waiting we rebuild the
     WAIT-FOR GRAPH (edge Ti -> Tj when Ti waits for a row Tj holds) and look
     for a cycle with depth-first search. A cycle = deadlock.
  2. InnoDB's own detector finds the same cycle, picks a VICTIM, rolls it
     back and returns error 1213 to it.

Recovery: the victim's locks are released, so the other transaction commits;
the victim is then restarted, exactly like transaction_manager._with_retry.

Nothing is changed in the database: the transactions only take locks
(SELECT ... FOR UPDATE) and commit.

    python deadlock_lab.py
"""
import threading
import time

import pymysql

import db

HOLD_MS = 250            # how long T1 keeps its first lock in the ordered run (makes the wait visible)


def find_cycle(edges):
    """
    Depth-first search for a cycle in a directed graph {node: [nodes it waits for]}.
    Returns the cycle as a path that starts and ends at the same node
    (e.g. ["T1", "T2", "T1"]), or None.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {n: WHITE for n in edges}
    for targets in edges.values():
        for t in targets:
            colour.setdefault(t, WHITE)
    stack = []

    def visit(n):
        colour[n] = GREY
        stack.append(n)
        for m in edges.get(n, []):
            if colour[m] == GREY:                       # back edge -> cycle
                return stack[stack.index(m):] + [m]
            if colour[m] == WHITE:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        colour[n] = BLACK
        return None

    for n in sorted(colour):
        if colour[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


class LockTable:
    """Shared table of who holds which row and who waits for which: the wait-for graph."""

    def __init__(self, names):
        self.t0 = time.perf_counter()
        self.lock = threading.Lock()
        self.names = names                  # row key -> readable name
        self.holds = {}                     # row -> transaction
        self.waits = {}                     # transaction -> row
        self.events = []
        self.cycle = None
        self.victim = None
        self.graph = {"edges": []}

    def _ms(self):
        return round((time.perf_counter() - self.t0) * 1000, 1)

    def log(self, tx, kind, text):
        with self.lock:
            self.events.append({"t": self._ms(), "tx": tx, "kind": kind, "text": text})

    def edges(self):
        return {tx: [self.holds[row]] for tx, row in self.waits.items() if row in self.holds}

    def want(self, tx, row):
        """Called just BEFORE asking MySQL for the lock."""
        with self.lock:
            holder = self.holds.get(row)
            if holder is None or holder == tx:
                return
            self.waits[tx] = row
            self.events.append({"t": self._ms(), "tx": tx, "kind": "wait",
                                "text": f"waits for {self.names[row]} (held by {holder})"})
            edges = self.edges()
            self.graph = {"edges": [{"from": a, "to": b, "row": self.names[self.waits[a]]}
                                    for a, targets in edges.items() for b in targets]}
            cycle = find_cycle(edges)
            if cycle and not self.cycle:
                self.cycle = cycle
                self.events.append({"t": self._ms(), "tx": "detector", "kind": "cycle",
                                    "text": "cycle in the wait-for graph: " + " -> ".join(cycle)
                                            + "  =>  DEADLOCK"})

    def got(self, tx, row):
        with self.lock:
            prev = self.holds.get(row)
            if prev and prev != tx:
                # we got a row another transaction still "held": InnoDB rolled that one back
                self.victim = prev
                for r in [r for r, holder in self.holds.items() if holder == prev]:
                    del self.holds[r]
                self.waits.pop(prev, None)
                self.events.append({"t": self._ms(), "tx": "InnoDB", "kind": "victim",
                                    "text": f"picks {prev} as the VICTIM: ROLLBACK, its locks are released"})
            self.waits.pop(tx, None)
            self.holds[row] = tx
            self.events.append({"t": self._ms(), "tx": tx, "kind": "lock", "text": f"locked {self.names[row]}"})

    def release_all(self, tx, why):
        with self.lock:
            self.waits.pop(tx, None)
            for row in [r for r, holder in self.holds.items() if holder == tx]:
                del self.holds[row]
            self.events.append({"t": self._ms(), "tx": tx, "kind": why,
                                "text": {"commit": "COMMIT - locks released",
                                         "victim": "gets error 1213 (deadlock): its transaction was rolled back",
                                         "timeout": "gets error 1205 (lock wait timeout): ROLLBACK, locks released",
                                         }[why]})
            if why != "commit":
                self.victim = self.victim or tx


def _lab_rows():
    rows = db.query_all("SELECT r.resource_id, r.resource_type FROM resources r "
                        "JOIN hospitals h ON h.hospital_id = r.hospital_id "
                        "WHERE h.is_lab = 1 AND r.resource_type IN ('ICU_BED', 'AMBULANCE')")
    by_type = {r["resource_type"]: r["resource_id"] for r in rows}
    if len(by_type) < 2:
        raise RuntimeError("Lab hospital rows missing - run `python init_db.py` first")
    return by_type["ICU_BED"], by_type["AMBULANCE"]


def run(ordered=False):
    a, b = _lab_rows()
    names = {a: f"A (ICU-bed counter, resources #{a})", b: f"B (ambulance counter, resources #{b})"}
    table = LockTable(names)
    plan = {"T1": [a, b], "T2": [a, b] if ordered else [b, a]}
    first_locked = threading.Barrier(2)            # unordered: both hold their first row
    t1_has_a = threading.Event()                   # ordered: T2 starts after T1 holds A
    outcome = {}

    def lock_row(cur, tx, row):
        table.want(tx, row)
        cur.execute("SELECT available_units FROM resources WHERE resource_id = %s FOR UPDATE", (row,))
        table.got(tx, row)

    def transaction(tx, attempt):
        conn = db.get_connection()
        try:
            conn.begin()
            table.log(tx, "begin", "BEGIN" + (f" (retry #{attempt - 1})" if attempt > 1 else "")
                      + f" - needs {' then '.join(names[r][0] for r in plan[tx])}")
            with conn.cursor() as cur:
                if ordered and tx == "T2" and attempt == 1:
                    t1_has_a.wait(5)
                lock_row(cur, tx, plan[tx][0])
                if attempt == 1:
                    if ordered and tx == "T1":
                        t1_has_a.set()
                        time.sleep(HOLD_MS / 1000)          # T2 is now waiting for A
                    elif not ordered:
                        first_locked.wait(5)
                        if tx == "T2":
                            time.sleep(0.05)                 # T1 asks first, T2 closes the cycle
                lock_row(cur, tx, plan[tx][1])
            conn.commit()
            table.release_all(tx, "commit")
            return "committed"
        except pymysql.err.OperationalError as e:
            conn.rollback()
            code = e.args[0] if e.args else None
            if code == db.ER_LOCK_DEADLOCK:
                table.release_all(tx, "victim")
                return "victim"
            if code == db.ER_LOCK_WAIT_TIMEOUT:
                table.release_all(tx, "timeout")
                return "victim"
            raise
        finally:
            conn.close()

    def worker(tx):
        for attempt in (1, 2, 3):
            result = transaction(tx, attempt)
            if result == "committed":
                outcome[tx] = "committed" if attempt == 1 else f"committed after {attempt - 1} retry"
                return
            time.sleep(0.05)                          # back off, then restart (recovery)
        outcome[tx] = "gave up"

    threads = [threading.Thread(target=worker, args=(tx,)) for tx in ("T1", "T2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    victim = table.victim
    label = ("ORDERED: both lock A then B (global lock order)" if ordered
             else "OPPOSITE ORDER: T1 locks A then B, T2 locks B then A")
    if ordered:
        verdict = "No deadlock: T2 waited for T1, the wait-for graph never had a cycle."
    elif table.cycle:
        verdict = (f"Deadlock detected ({' -> '.join(table.cycle)}). InnoDB rolled back {victim}; "
                   f"the other committed, then {victim} was retried and committed.")
    else:
        verdict = "No deadlock this time (the timing did not overlap)."
    db.execute("INSERT INTO audit_log (event_type, details) VALUES ('LAB_DEADLOCK', %s)",
               (f"{label}: {verdict}"[:500],))
    return {"ordered": ordered, "label": label, "rows": {"A": names[a], "B": names[b]},
            "plan": {tx: [names[r][0] for r in rows] for tx, rows in plan.items()},
            "events": sorted(table.events, key=lambda e: e["t"]),
            "graph": table.graph, "cycle": table.cycle, "deadlock": bool(table.cycle),
            "victim": victim, "outcome": outcome, "verdict": verdict}


if __name__ == "__main__":
    for ordered in (False, True):
        r = run(ordered)
        print(f"\n=== {r['label']} ===")
        for e in r["events"]:
            print(f"  {e['t']:7.1f} ms  {e['tx']:8}  {e['text']}")
        print(f"  -> {r['verdict']}")
