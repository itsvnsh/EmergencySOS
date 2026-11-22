-- ============================================================
-- EmergencySOS - views, triggers and stored procedures (MySQL 8)
-- Team Rescuers (OSDBMS-V-2026-T203)
--
-- Loaded by init_db.py right after schema.sql. To load by hand:
--   mysql -u root -p emergencysos < schema_objects.sql
-- (the DELIMITER lines are understood by the mysql client and
--  by init_db.py)
--
-- VIEWS       stored queries the app and the Database tab read
-- TRIGGERS    rules the DATABASE enforces by itself, whoever
--             writes the row (app, terminal script or mysql shell)
-- PROCEDURES  the booking transaction written in pure SQL
-- ============================================================

DROP VIEW IF EXISTS v_hospital_capacity;
DROP VIEW IF EXISTS v_doctor_load;
DROP VIEW IF EXISTS v_active_cases;
DROP VIEW IF EXISTS v_pending_queue;
DROP VIEW IF EXISTS v_hospital_performance;
DROP PROCEDURE IF EXISTS sp_allocate_unit;
DROP PROCEDURE IF EXISTS sp_hospital_report;

-- ============================================================
-- VIEWS
-- ============================================================

-- 1. Live capacity of every unit type in every hospital
CREATE VIEW v_hospital_capacity AS
SELECT h.hospital_id,
       h.name                                   AS hospital,
       r.resource_type,
       r.total_units,
       r.available_units,
       r.total_units - r.available_units        AS in_use,
       ROUND(100 * (r.total_units - r.available_units) / NULLIF(r.total_units, 0), 1) AS occupancy_pct
  FROM hospitals h
  JOIN resources r ON r.hospital_id = h.hospital_id
 WHERE h.is_lab = 0;

-- 2. How busy each doctor is
CREATE VIEW v_doctor_load AS
SELECT d.doctor_id,
       h.name                                   AS hospital,
       d.name                                   AS doctor,
       d.specialization,
       d.on_shift,
       d.current_cases,
       d.max_cases,
       CASE WHEN d.on_shift = 1 THEN d.max_cases - d.current_cases ELSE 0 END AS free_slots
  FROM doctors d
  JOIN hospitals h ON h.hospital_id = d.hospital_id
 WHERE h.is_lab = 0;

-- 3. Every case in progress, with time elapsed and time left
CREATE VIEW v_active_cases AS
SELECT a.allocation_id,
       r.request_id,
       r.patient_name,
       r.emergency_type,
       r.priority,
       h.name                                   AS hospital,
       a.hospital_id,
       r.required_resource                      AS unit,
       d.name                                   AS doctor,
       a.distance_km,
       a.allocated_at,
       TIMESTAMPDIFF(MINUTE, a.allocated_at, NOW())          AS minutes_elapsed,
       GREATEST(0, TIMESTAMPDIFF(MINUTE, NOW(), a.expected_end_at)) AS minutes_left,
       CASE WHEN a.ambulance_resource_id IS NULL THEN 'NONE'
            WHEN a.ambulance_returned_at IS NULL THEN 'ON ROAD'
            ELSE 'BACK AT BASE' END             AS ambulance
  FROM allocations a
  JOIN sos_requests r ON r.request_id = a.request_id
  JOIN hospitals h    ON h.hospital_id = a.hospital_id
  LEFT JOIN doctors d ON d.doctor_id = a.doctor_id
 WHERE a.status = 'ACTIVE' AND h.is_lab = 0;

-- 4. Waiting patients with their AGED priority, computed in SQL.
--    Same rule as scheduler.effective_priority(): every 10 minutes
--    of waiting raises the priority by one level (never above P1).
CREATE VIEW v_pending_queue AS
SELECT r.request_id,
       r.patient_name,
       r.emergency_type,
       r.severity,
       r.priority,
       TIMESTAMPDIFF(MINUTE, r.created_at, NOW())            AS waiting_min,
       GREATEST(1, r.priority - FLOOR(TIMESTAMPDIFF(MINUTE, r.created_at, NOW()) / 10)) AS effective_priority,
       r.required_resource,
       r.est_handling_min                       AS burst_min,
       r.created_at
  FROM sos_requests r
 WHERE r.status = 'PENDING' AND r.is_lab = 0;

-- 5. Per-hospital performance over every case it has taken
CREATE VIEW v_hospital_performance AS
SELECT h.hospital_id,
       h.name                                   AS hospital,
       COUNT(a.allocation_id)                   AS cases_total,
       COALESCE(SUM(a.status = 'ACTIVE'), 0)    AS cases_active,
       COALESCE(SUM(a.status = 'RELEASED'), 0)  AS cases_done,
       ROUND(AVG(TIMESTAMPDIFF(SECOND, r.created_at, a.allocated_at)) / 60, 1) AS avg_wait_min,
       ROUND(AVG(a.distance_km), 2)             AS avg_distance_km
  FROM hospitals h
  LEFT JOIN allocations a  ON a.hospital_id = h.hospital_id
  LEFT JOIN sos_requests r ON r.request_id = a.request_id
 WHERE h.is_lab = 0
 GROUP BY h.hospital_id, h.name;

-- ============================================================
-- TRIGGERS
-- ============================================================
DELIMITER $$

-- 1. Process state machine. An SOS request is a process, and a
--    process can only move along legal state transitions:
--        PENDING -> ALLOCATED -> COMPLETED
--        PENDING -> CANCELLED
--    Anything else (e.g. COMPLETED -> PENDING) is refused by the
--    database itself, even from a hand-typed UPDATE.
CREATE TRIGGER trg_request_state_guard
BEFORE UPDATE ON sos_requests
FOR EACH ROW
BEGIN
    DECLARE v_msg VARCHAR(128);
    IF NEW.status <> OLD.status AND NOT (
           (OLD.status = 'PENDING'   AND NEW.status IN ('ALLOCATED', 'CANCELLED'))
        OR (OLD.status = 'ALLOCATED' AND NEW.status = 'COMPLETED')
    ) THEN
        SET v_msg = CONCAT('Illegal request state change ', OLD.status, ' -> ', NEW.status,
                           ' (trigger trg_request_state_guard)');
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = v_msg;
    END IF;
END$$

-- 2. Status history: a new request is born PENDING. The database
--    writes the first row of its timeline by itself.
CREATE TRIGGER trg_request_history_insert
AFTER INSERT ON sos_requests
FOR EACH ROW
BEGIN
    INSERT INTO request_status_history (request_id, old_status, new_status)
    VALUES (NEW.request_id, NULL, NEW.status);
END$$

-- 3. Status history: every later status change adds one row
--    (old -> new, with a millisecond timestamp).
CREATE TRIGGER trg_request_history_update
AFTER UPDATE ON sos_requests
FOR EACH ROW
BEGIN
    IF NEW.status <> OLD.status THEN
        INSERT INTO request_status_history (request_id, old_status, new_status)
        VALUES (NEW.request_id, OLD.status, NEW.status);
    END IF;
END$$

-- 4. Cross-table consistency a CHECK constraint cannot express:
--    the unit, the doctor and the ambulance of an allocation must
--    all belong to the hospital the patient is booked into.
CREATE TRIGGER trg_allocation_same_hospital
BEFORE INSERT ON allocations
FOR EACH ROW
BEGIN
    IF NOT EXISTS (SELECT 1 FROM resources
                    WHERE resource_id = NEW.resource_id AND hospital_id = NEW.hospital_id
                      AND resource_type <> 'AMBULANCE') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Allocation unit does not belong to that hospital';
    END IF;
    IF NEW.doctor_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM doctors WHERE doctor_id = NEW.doctor_id AND hospital_id = NEW.hospital_id) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Allocation doctor does not work at that hospital';
    END IF;
    IF NEW.ambulance_resource_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM resources WHERE resource_id = NEW.ambulance_resource_id
              AND hospital_id = NEW.hospital_id AND resource_type = 'AMBULANCE') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Allocation ambulance does not belong to that hospital';
    END IF;
END$$

-- 5. Capacity alerts: when a unit type runs out (free 1 -> 0) or
--    becomes free again (0 -> 1+), the database writes an audit
--    row by itself. The live-update stream then shows it on every
--    open screen.
CREATE TRIGGER trg_resource_capacity_alert
AFTER UPDATE ON resources
FOR EACH ROW
BEGIN
    DECLARE v_name VARCHAR(120);
    DECLARE v_lab TINYINT;
    IF (OLD.available_units > 0 AND NEW.available_units = 0)
       OR (OLD.available_units = 0 AND NEW.available_units > 0) THEN
        SELECT name, is_lab INTO v_name, v_lab FROM hospitals WHERE hospital_id = NEW.hospital_id;
        IF v_lab = 0 THEN
            INSERT INTO audit_log (event_type, hospital_id, details)
            VALUES (IF(NEW.available_units = 0, 'CAPACITY_ALERT', 'CAPACITY_RESTORED'),
                    NEW.hospital_id,
                    CONCAT(v_name, ': ', REPLACE(NEW.resource_type, '_', ' '),
                           IF(NEW.available_units = 0, ' - none free (trigger)',
                              CONCAT(' - ', NEW.available_units, ' free again (trigger)'))));
        END IF;
    END IF;
END$$

-- ============================================================
-- STORED PROCEDURES
-- ============================================================

-- 1. The booking transaction in pure SQL - the same steps and the
--    same lock order as transaction_manager._try_reserve():
--      request -> care unit -> doctor -> ambulance -> allocation
--    The concurrency lab can race clients through this procedure
--    instead of the Python code; the result is the same (one winner).
--    p_time_scale : like CASE_TIME_SCALE (1 = real minutes)
--    p_delay_ms   : pause after locking the unit (lab only)
--    p_result     : allocated | not_found | not_pending | no_unit | no_ambulance
CREATE PROCEDURE sp_allocate_unit(
    IN  p_request_id    INT,
    IN  p_hospital_id   INT,
    IN  p_time_scale    DECIMAL(8,3),
    IN  p_delay_ms      INT,
    OUT p_result        VARCHAR(20),
    OUT p_allocation_id INT)
proc: BEGIN
    DECLARE v_status   VARCHAR(20) DEFAULT NULL;
    DECLARE v_unit     VARCHAR(30);
    DECLARE v_spec     VARCHAR(30);
    DECLARE v_burst    INT;
    DECLARE v_unit_id  INT DEFAULT NULL;
    DECLARE v_unit_free INT DEFAULT 0;
    DECLARE v_doc_id   INT DEFAULT NULL;
    DECLARE v_amb_id   INT DEFAULT NULL;
    DECLARE v_amb_free INT DEFAULT 0;
    DECLARE v_case_sec INT;

    -- any SQL error (deadlock, lock timeout, a trigger refusing the row):
    -- undo everything, then pass the error on to the caller
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;

    SET p_allocation_id = NULL;
    SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
    START TRANSACTION;

    -- (1) lock the request
    SELECT status, required_resource, required_specialist, est_handling_min
      INTO v_status, v_unit, v_spec, v_burst
      FROM sos_requests WHERE request_id = p_request_id FOR UPDATE;
    IF v_status IS NULL THEN
        ROLLBACK; SET p_result = 'not_found'; LEAVE proc;
    END IF;
    IF v_status <> 'PENDING' THEN
        ROLLBACK; SET p_result = 'not_pending'; LEAVE proc;
    END IF;

    -- (2) lock the unit counter and re-check it
    SELECT resource_id, available_units INTO v_unit_id, v_unit_free
      FROM resources WHERE hospital_id = p_hospital_id AND resource_type = v_unit FOR UPDATE;
    IF p_delay_ms > 0 THEN
        DO SLEEP(p_delay_ms / 1000);
    END IF;
    IF v_unit_id IS NULL OR v_unit_free <= 0 THEN
        ROLLBACK; SET p_result = 'no_unit'; LEAVE proc;
    END IF;

    -- (3) lock one doctor: the specialist, else a general physician
    SELECT doctor_id INTO v_doc_id FROM doctors
     WHERE hospital_id = p_hospital_id AND specialization = v_spec
       AND on_shift = 1 AND current_cases < max_cases
     ORDER BY current_cases, doctor_id LIMIT 1 FOR UPDATE;
    IF v_doc_id IS NULL AND v_spec <> 'GENERAL_PHYSICIAN' THEN
        SELECT doctor_id INTO v_doc_id FROM doctors
         WHERE hospital_id = p_hospital_id AND specialization = 'GENERAL_PHYSICIAN'
           AND on_shift = 1 AND current_cases < max_cases
         ORDER BY current_cases, doctor_id LIMIT 1 FOR UPDATE;
    END IF;

    -- (4) lock the ambulance fleet
    SELECT resource_id, available_units INTO v_amb_id, v_amb_free
      FROM resources WHERE hospital_id = p_hospital_id AND resource_type = 'AMBULANCE' FOR UPDATE;
    IF v_amb_id IS NULL OR v_amb_free <= 0 THEN
        ROLLBACK; SET p_result = 'no_ambulance'; LEAVE proc;
    END IF;

    -- (5) all checks passed: write everything, then COMMIT
    SET v_case_sec = GREATEST(1, ROUND(v_burst * 60 * p_time_scale));
    UPDATE resources SET available_units = available_units - 1 WHERE resource_id = v_unit_id;
    UPDATE resources SET available_units = available_units - 1 WHERE resource_id = v_amb_id;
    IF v_doc_id IS NOT NULL THEN
        UPDATE doctors SET current_cases = current_cases + 1 WHERE doctor_id = v_doc_id;
    END IF;
    UPDATE sos_requests SET status = 'ALLOCATED' WHERE request_id = p_request_id;
    INSERT INTO allocations (request_id, hospital_id, resource_id, doctor_id, expected_end_at,
                             ambulance_resource_id, ambulance_due_at)
    VALUES (p_request_id, p_hospital_id, v_unit_id, v_doc_id,
            NOW() + INTERVAL v_case_sec SECOND, v_amb_id,
            NOW() + INTERVAL LEAST(v_case_sec, ROUND(20 * 60 * p_time_scale)) SECOND);
    SET p_allocation_id = LAST_INSERT_ID();
    INSERT INTO audit_log (event_type, request_id, hospital_id, details)
    VALUES ('ALLOCATED', p_request_id, p_hospital_id,
            CONCAT('Request #', p_request_id, ' -> unit #', v_unit_id,
                   ' (booked by stored procedure sp_allocate_unit)'));
    COMMIT;
    SET p_result = 'allocated';
END$$

-- 2. One hospital at a glance: returns TWO result sets
--    (unit capacity, then doctor load), read from the views above.
CREATE PROCEDURE sp_hospital_report(IN p_hospital_id INT)
BEGIN
    SELECT resource_type, total_units, available_units, in_use, occupancy_pct
      FROM v_hospital_capacity WHERE hospital_id = p_hospital_id
     ORDER BY resource_type;
    SELECT l.doctor, l.specialization, l.on_shift, l.current_cases, l.max_cases, l.free_slots
      FROM v_doctor_load l
      JOIN doctors d ON d.doctor_id = l.doctor_id
     WHERE d.hospital_id = p_hospital_id
     ORDER BY l.specialization, l.doctor;
END$$

DELIMITER ;
