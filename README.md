# EmergencySOS

**Team Rescuers · OSDBMS-V-2026-T203 · B.Tech CSE, Graphic Era (Deemed to be University)**

EmergencySOS routes an emergency (SOS) request to the most suitable hospital based on live bed, equipment and doctor availability. It combines two subjects:

| Subject | Idea | Where in the code |
|---|---|---|
| **Operating Systems** | SOS requests are *processes*; the dispatcher is the *CPU*; beds, ventilators and doctors are *shared resources*. Requests are ordered by **Priority with aging**, **FCFS**, **SJF** or **Multilevel Queue**. | `scheduler.py` |
| **DBMS** | Hospital state lives in MySQL (InnoDB). Every reservation is an **ACID transaction** at **REPEATABLE READ**, using `SELECT ... FOR UPDATE` row locks for mutual exclusion, a fixed lock order against deadlocks, and rollback and retry. | `schema.sql`, `transaction_manager.py` |

Hospital names are real Dehradun-region hospitals with approximate locations. **All bed, equipment and doctor data is simulated.**

---

## 1. Project structure

```
EmergencySOS/
├── schema.sql              # 6 tables, foreign keys, CHECK constraints, indexes
├── setup_mysql.py          # one-time: creates databases + an app MySQL user (no root needed after)
├── init_db.py              # creates the database and loads simulated data
├── config.py               # DB login, isolation level, aging interval, ranking weights
├── db.py                   # PyMySQL connection helpers + audit-log helper
├── classifier.py           # Emergency Classifier: type + severity -> priority, resource, specialist, burst
├── scheduler.py            # Priority (aging), FCFS, SJF, MLQ + metrics + Gantt output
├── ranking.py              # weighted hospital scoring (distance/units/specialist/load)
├── transaction_manager.py  # locking, commit/rollback, lock ordering, deadlock retry, release, admin updates
├── concurrency_lab.py      # race demo (terminal): N clients vs 1 ICU bed, with and without locks
├── audit_log.py            # prints the latest audit-log events (terminal)
├── app.py                  # Flask REST API + serves the web console
├── templates/index.html    # web console
├── static/style.css
├── static/app.js
├── sample_workload.json    # the 12-request workload used in the report
├── test_core.py            # automated tests (32 tests)
└── requirements.txt
```

### Request flow

```
SOS form ──► Classifier ──► OS Scheduler ──► Ranking ──► Transaction Manager ──► MySQL
 (type,       (priority     (Priority/aging,  (40% dist,   (lock, re-check,        (beds, doctors,
  severity,    1-5, unit,    FCFS, SJF, MLQ)   25% units,   commit / rollback)      allocations, log)
  location)    specialist,                     20% spec,         │
               burst)                          15% load)         └─ unit gone? rollback → next hospital
```

---

## 2. Setup (do this once)

### 2.1 Install the software
1. **Python 3.10+**: https://www.python.org/downloads/ (on Windows, tick "Add Python to PATH").
2. **MySQL 8 Community Server**: https://dev.mysql.com/downloads/installer/. Remember the **root password** you set.
3. Make sure the MySQL service is running. On Windows, check Services → MySQL80 → Running.

### 2.2 Install the Python packages
Open a terminal in the `EmergencySOS` folder:

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux / macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2.3 Easiest: run the setup script
```bash
python setup_mysql.py
```
The script asks once for an admin login: root, or whoever can create users. With XAMPP, root has no password, so just press Enter. It then:
- creates the `emergencysos` and `emergencysos_test` databases
- creates an app user (`sos` by default) with rights on those two databases only
- saves that login to `db_login.json`, which `config.py` reads automatically

The admin password is never saved. After this, skip to 2.4.

### 2.3b Or set the login by hand
Either edit `config.py` (`DB_PASSWORD = os.environ.get("DB_PASSWORD", "your_password")`) **or** set an environment variable:

```bash
# Windows PowerShell
$env:DB_PASSWORD="your_password"
# Linux / macOS
export DB_PASSWORD=your_password
```

`DB_HOST`, `DB_PORT`, `DB_USER` and `DB_NAME` can be set the same way. The defaults are `localhost`, `3306`, `root` and `emergencysos`.

### 2.4 Create the database
```bash
python init_db.py
```
This creates the `emergencysos` database, all tables and the simulated data: 8 hospitals, 39 doctors, and one hidden "Concurrency Lab Hospital".
Run it again any time to reset everything. The **Reset simulated data** button in the console does the same.

---

## 3. Run

```bash
python app.py
```
Open **http://127.0.0.1:5000** in your browser.

Run the tests (they use a separate `emergencysos_test` database, so demo data is safe):
```bash
python -m unittest -v test_core
```

Back-end tools (run in the terminal):
```bash
python scheduler.py         # prints the scheduler comparison for the sample workload
python concurrency_lab.py   # runs the race unlocked and locked
python audit_log.py         # latest 40 audit-log events (python audit_log.py 100 for more)
```

---

## 4. Demo script (for the evaluation)

1. **SOS & Dispatch tab**: fill the form, for example *Cardiac, severity 5, Graphic Era University*, and click **Send SOS**. The classifier result appears: P1, ICU bed + cardiologist, 13 min handling time, with the top three matching hospitals.
2. **Queue tab**: click **Add demo requests**. Six requests appear that "arrived" 1–45 minutes ago. Switch the policy dropdown between Priority, FCFS, SJF and MLQ to see the order change. Old low-priority requests show an **aged** tag because their effective priority improved while they waited.
3. Back on **SOS & Dispatch**, click **Dispatch next**. The result shows the chosen hospital, the doctor, the ranking table with scores, and the transaction attempts.
4. **Admin changes affect routing**: on **Hospitals & Admin**, set the top-ranked hospital's free ICU beds to 0, then dispatch another cardiac case. That hospital drops out of the ranking. The *stale ranking* case, where a hospital fills up **between** ranking and commit, is a timing race. It is shown by the automated test `test_stale_ranking_falls_back`: the locked re-check fails, the transaction rolls back, and the next hospital is booked. When it happens live, the attempts list shows "unit gone – rolled back".
5. **Release** an allocation. The unit goes back to the hospital and the doctor's case count drops.
6. **Scheduler Lab**: click **Run on sample workload**. It shows the metrics table and a Gantt chart for each policy. Expected numbers:

   | Policy | Avg wait | Avg turnaround | Critical (P1–P2) wait | Max wait |
   |---|---|---|---|---|
   | Priority with aging | 24.92 | 34.50 | **18.75** | 79 |
   | Priority (no aging) | 25.92 | 35.50 | 12.75 | 80 |
   | FCFS | 25.33 | 34.92 | **27.88** | 59 |
   | SJF | 23.92 | 33.50 | 22.62 | 65 |
   | Multilevel Queue | 25.92 | 35.50 | 12.75 | 80 |

   Talking point: aging trades a little critical-patient wait (12.75 → 18.75) for fairness. The longest wait drops and low-priority requests cannot starve.
7. **Concurrency race (terminal)**: run `python concurrency_lab.py`. The unlocked run gives 6 bookings for 1 bed (INCONSISTENT); the locked run gives exactly 1 booking and 5 rollbacks.
8. **Audit log (terminal)**: run `python audit_log.py` to list every SOS, allocation, rollback, release, admin change and lab run, each written inside the same transaction as the change.

---

## 5. Key design decisions

**Classifier rules** (`classifier.py`)
- Base priority = `6 − severity` (severity 5 → P1).
- Life-threatening types (cardiac, stroke, respiratory) move up one level when severity ≥ 3.
- The required resource escalates with severity. For example, respiratory sev ≤ 2 needs a general bed, sev 3 an ICU bed, and sev ≥ 4 a ventilator.
- **Burst time** = estimated dispatcher handling time = base minutes for the type + severity.

**Scheduling** (`scheduler.py`)
- Non-preemptive, single dispatcher.
- Aging: `effective = priority − ⌊wait / 10 min⌋`, never below 1. Ties go to the originally more urgent request, so an aged minor case never overtakes a genuine P1.
- MLQ uses three queues with fixed priority between them: Q1 Critical (P1–P2) by priority, Q2 Urgent (P3) FCFS, and Q3 Routine (P4–P5) SJF.

**Ranking** (`ranking.py`): `score = 100 × (0.40·distance + 0.25·units + 0.20·specialist + 0.15·load)`
- Distance and units are normalised across the candidate hospitals.
- Specialist = 1 if the required specialist is on shift, 0.5 if only a general physician is available, and 0 otherwise.
- Load = 1 − occupancy.
- Distance is straight-line (Haversine).

**Transactions** (`transaction_manager.py`)
- Each reservation runs under REPEATABLE READ.
- Rows are locked in the order request → resource → doctor (→ allocation on release). The same order everywhere means no circular wait.
- Deadlock (1213) and lock-wait timeout (1205) errors are retried up to 3 times with back-off.
- Database CHECK constraints are a second safety net: `0 ≤ available_units ≤ total_units` and `0 ≤ current_cases ≤ max_cases`.

**Race demo** (`concurrency_lab.py`)
- The unlocked version is a plain read → 50 ms pause → write, with no transaction. That is a classic lost update.
- The locked version calls the real `transaction_manager.reserve()`.

---

## 6. REST API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/meta` | emergency types, policies, preset locations |
| POST | `/api/sos` | create + classify an SOS request |
| GET | `/api/queue?policy=PRIORITY` | pending queue ordered by a policy |
| GET | `/api/rank/<id>` | ranked hospitals for a request |
| POST | `/api/dispatch` | `{policy}` serve the head of the queue, or `{request_id}` |
| GET | `/api/requests` | recent requests |
| GET | `/api/allocations?status=ACTIVE` | allocations |
| POST | `/api/allocations/<id>/release` | finish a case |
| GET | `/api/hospitals` | capacity dashboard |
| POST | `/api/admin/resource` | change unit counts |
| POST | `/api/admin/doctor/<id>/shift` | toggle doctor shift |
| POST | `/api/admin/reset` | reload simulated data |
| GET | `/api/audit` | audit log |
| POST | `/api/demo/seed` | add 6 demo requests |
| POST | `/api/labs/scheduler` | compare policies |
| POST | `/api/labs/race` | `{locked, clients, delay_ms}` run the race |

---

## 7. Status (Phase II, about 70%)

**Done:** schema and data loader, classifier, four schedulers with metrics and Gantt output, ranking, transaction manager, concurrency lab, REST API, web console, audit log, automated tests.

**Pending (Phase III):**
- login and roles (patient, dispatcher, hospital admin) and protection of the admin APIs
- load testing with many clients and tuning of locking and isolation
- map view and road-distance routing
- automatic case completion and live push updates (the console currently polls every 5 s)
- mobile-friendly SOS portal
- ER diagram, final report and demo video

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `Access denied for user 'root'` | Wrong password. Set `DB_PASSWORD` (section 2.3). |
| `Can't connect to MySQL server` | The MySQL service is not running. Start it. |
| `Unknown database 'emergencysos'` | Run `python init_db.py`. |
| `'cryptography' package is required` | Run `pip install -r requirements.txt` again. |
| Port 5000 busy | Set `FLASK_PORT` to another port, e.g. `$env:FLASK_PORT="5050"`. |
