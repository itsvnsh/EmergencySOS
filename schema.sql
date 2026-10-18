-- ============================================================
-- EmergencySOS - relational schema (MySQL 8, InnoDB)
-- Team Rescuers (OSDBMS-V-2026-T203)
--
-- Run through init_db.py, or manually:
--   mysql -u root -p emergencysos < schema.sql
-- All tables use InnoDB, which gives ACID transactions and
-- row-level locking (needed for SELECT ... FOR UPDATE).
-- ============================================================

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS allocations;
DROP TABLE IF EXISTS sos_requests;
DROP TABLE IF EXISTS doctors;
DROP TABLE IF EXISTS resources;
DROP TABLE IF EXISTS hospitals;
SET FOREIGN_KEY_CHECKS = 1;

-- ------------------------------------------------------------
-- 1. Hospitals (simulated data, real names, approximate locations)
-- ------------------------------------------------------------
CREATE TABLE hospitals (
    hospital_id   INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(120) NOT NULL UNIQUE,
    area          VARCHAR(80)  NOT NULL,
    latitude      DECIMAL(9,6) NOT NULL,
    longitude     DECIMAL(9,6) NOT NULL,
    phone         VARCHAR(20),
    is_lab        TINYINT(1)   NOT NULL DEFAULT 0,  -- 1 = only used by the concurrency lab
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_hosp_lat CHECK (latitude  BETWEEN -90  AND 90),
    CONSTRAINT chk_hosp_lon CHECK (longitude BETWEEN -180 AND 180)
) ENGINE = InnoDB;

-- ------------------------------------------------------------
-- 2. Resources = shared, countable units (beds, ventilators, OTs)
--    In OS terms these are the shared resources that processes
--    (SOS requests) compete for.
-- ------------------------------------------------------------
CREATE TABLE resources (
    resource_id      INT AUTO_INCREMENT PRIMARY KEY,
    hospital_id      INT NOT NULL,
    resource_type    ENUM('ICU_BED','VENTILATOR','OPERATION_THEATRE','GENERAL_BED') NOT NULL,
    total_units      INT NOT NULL,
    available_units  INT NOT NULL,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_res_hospital FOREIGN KEY (hospital_id)
        REFERENCES hospitals(hospital_id) ON DELETE CASCADE,
    CONSTRAINT uq_res_hosp_type UNIQUE (hospital_id, resource_type),
    -- a hospital can never have negative units or more free units than it owns
    CONSTRAINT chk_res_total CHECK (total_units >= 0),
    CONSTRAINT chk_res_avail CHECK (available_units >= 0 AND available_units <= total_units)
) ENGINE = InnoDB;

CREATE INDEX idx_res_type_avail ON resources (resource_type, available_units);

-- ------------------------------------------------------------
-- 3. Doctors / specialists
-- ------------------------------------------------------------
CREATE TABLE doctors (
    doctor_id       INT AUTO_INCREMENT PRIMARY KEY,
    hospital_id     INT NOT NULL,
    name            VARCHAR(80) NOT NULL,
    specialization  ENUM('CARDIOLOGIST','NEUROLOGIST','TRAUMA_SURGEON','PULMONOLOGIST',
                         'BURN_SPECIALIST','GYNECOLOGIST','GENERAL_PHYSICIAN') NOT NULL,
    on_shift        TINYINT(1) NOT NULL DEFAULT 1,
    max_cases       INT NOT NULL DEFAULT 3,
    current_cases   INT NOT NULL DEFAULT 0,
    CONSTRAINT fk_doc_hospital FOREIGN KEY (hospital_id)
        REFERENCES hospitals(hospital_id) ON DELETE CASCADE,
    CONSTRAINT chk_doc_max   CHECK (max_cases > 0),
    CONSTRAINT chk_doc_cases CHECK (current_cases >= 0 AND current_cases <= max_cases)
) ENGINE = InnoDB;

CREATE INDEX idx_doc_lookup ON doctors (hospital_id, specialization, on_shift);

-- ------------------------------------------------------------
-- 4. SOS requests = the "processes" handled by the scheduler
--    severity : 1 (mild) .. 5 (critical)       - entered by the user
--    priority : 1 (highest) .. 5 (lowest)      - set by the classifier
-- ------------------------------------------------------------
CREATE TABLE sos_requests (
    request_id           INT AUTO_INCREMENT PRIMARY KEY,
    patient_name         VARCHAR(80) NOT NULL,
    phone                VARCHAR(20),
    latitude             DECIMAL(9,6) NOT NULL,
    longitude            DECIMAL(9,6) NOT NULL,
    emergency_type       ENUM('CARDIAC','STROKE','TRAUMA','RESPIRATORY','BURN',
                              'MATERNITY','POISONING','GENERAL') NOT NULL,
    severity             TINYINT NOT NULL,
    description          VARCHAR(255),
    priority             TINYINT NOT NULL,
    required_resource    ENUM('ICU_BED','VENTILATOR','OPERATION_THEATRE','GENERAL_BED') NOT NULL,
    required_specialist  ENUM('CARDIOLOGIST','NEUROLOGIST','TRAUMA_SURGEON','PULMONOLOGIST',
                              'BURN_SPECIALIST','GYNECOLOGIST','GENERAL_PHYSICIAN') NOT NULL,
    est_handling_min     INT NOT NULL,             -- "burst time" used by SJF
    status               ENUM('PENDING','ALLOCATED','COMPLETED','CANCELLED') NOT NULL DEFAULT 'PENDING',
    is_lab               TINYINT(1) NOT NULL DEFAULT 0,
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT chk_req_severity CHECK (severity BETWEEN 1 AND 5),
    CONSTRAINT chk_req_priority CHECK (priority BETWEEN 1 AND 5),
    CONSTRAINT chk_req_burst    CHECK (est_handling_min > 0),
    CONSTRAINT chk_req_lat CHECK (latitude  BETWEEN -90  AND 90),
    CONSTRAINT chk_req_lon CHECK (longitude BETWEEN -180 AND 180)
) ENGINE = InnoDB;

-- the scheduler always reads "pending requests ordered by priority/arrival"
CREATE INDEX idx_req_queue ON sos_requests (status, is_lab, priority, created_at);

-- ------------------------------------------------------------
-- 5. Allocations = which hospital/unit/doctor a request received
-- ------------------------------------------------------------
CREATE TABLE allocations (
    allocation_id  INT AUTO_INCREMENT PRIMARY KEY,
    request_id     INT NOT NULL,
    hospital_id    INT NOT NULL,
    resource_id    INT NOT NULL,
    doctor_id      INT NULL,
    score          DECIMAL(6,2),
    distance_km    DECIMAL(7,2),
    status         ENUM('ACTIVE','RELEASED') NOT NULL DEFAULT 'ACTIVE',
    allocated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at    TIMESTAMP NULL,
    CONSTRAINT fk_alloc_request  FOREIGN KEY (request_id)  REFERENCES sos_requests(request_id) ON DELETE CASCADE,
    CONSTRAINT fk_alloc_hospital FOREIGN KEY (hospital_id) REFERENCES hospitals(hospital_id)   ON DELETE CASCADE,
    CONSTRAINT fk_alloc_resource FOREIGN KEY (resource_id) REFERENCES resources(resource_id)   ON DELETE CASCADE,
    CONSTRAINT fk_alloc_doctor   FOREIGN KEY (doctor_id)   REFERENCES doctors(doctor_id)       ON DELETE SET NULL,
    CONSTRAINT chk_alloc_dist CHECK (distance_km IS NULL OR distance_km >= 0)
) ENGINE = InnoDB;

CREATE INDEX idx_alloc_status ON allocations (status, hospital_id);

-- ------------------------------------------------------------
-- 6. Audit log - every important event, written inside the same
--    transaction as the change it describes
-- ------------------------------------------------------------
CREATE TABLE audit_log (
    log_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_type   VARCHAR(40) NOT NULL,
    request_id   INT NULL,
    hospital_id  INT NULL,
    details      VARCHAR(500),
    created_at   TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE = InnoDB;

CREATE INDEX idx_audit_time ON audit_log (created_at);
