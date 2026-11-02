-- Silver: one clean, typed row per real thing, rebuilt from bronze on every run, so it's safe
-- to re-run after new files load. Every source's quirks are handled here and nowhere else.
-- Times: *_local is plant wall-clock time (America/New_York), *_utc is UTC; both TIMESTAMP_NTZ.
-- Tested on the local emulator (plantgen emulate); NOT YET RUN against Snowflake.

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;
USE DATABASE plant;
USE SCHEMA silver;

-- Every spelling of every machine, keyed without case, spaces or punctuation:
-- "Coater 1", "COATER-01" and "coater1" all match C1.
CREATE OR REPLACE TABLE equipment_alias AS
SELECT DISTINCT e.code AS equipment,
       UPPER(REGEXP_REPLACE(a.value::STRING, '[^A-Za-z0-9]', '')) AS alias_key
FROM bronze.ref_equipment e,
     LATERAL FLATTEN(input => SPLIT(e.aliases || '|' || e.code, '|')) a;

-- Canonical downtime reasons (99 OTHER has no reason, so it isn't one).
CREATE OR REPLACE TABLE reasons AS
SELECT reason, hmi_code, hmi_text, planned
FROM bronze.ref_downtime_codes
WHERE reason IS NOT NULL;

-- How QC inspectors spell each defect type, keyed like equipment_alias (letters only).
-- Built by looking at SELECT DISTINCT defect_type; an unmapped spelling fails 06_check.sql.
CREATE OR REPLACE TABLE defect_type_map AS
SELECT column1 AS spelling_key, column2 AS defect_type
FROM VALUES
  ('STARTUPWASTE', 'STARTUP_WASTE'), ('STARTUP', 'STARTUP_WASTE'),
  ('PINHOLES', 'PINHOLES'), ('PINHOLE', 'PINHOLES'),
  ('STREAKS', 'STREAKS'), ('STREAKING', 'STREAKS'), ('STREAK', 'STREAKS'),
  ('COATWEIGHT', 'COAT_WEIGHT'), ('COATWT', 'COAT_WEIGHT'), ('CWOUTOFSPEC', 'COAT_WEIGHT'),
  ('WRINKLE', 'WRINKLE'), ('WRINKLES', 'WRINKLE'), ('WRINKLED', 'WRINKLE'),
  ('SPLICEWASTE', 'SPLICE_WASTE'), ('SPLICE', 'SPLICE_WASTE'),
  ('CONTAMINATION', 'CONTAMINATION'), ('CONTAM', 'CONTAMINATION'), ('DIRTGELS', 'CONTAMINATION'),
  ('ADHESIVEVOIDS', 'ADHESIVE_VOIDS'), ('VOIDS', 'ADHESIVE_VOIDS'), ('ADHVOIDS', 'ADHESIVE_VOIDS'),
  ('EDGEFUZZ', 'EDGE_FUZZ'), ('FUZZYEDGES', 'EDGE_FUZZ'), ('EDGEQUALITY', 'EDGE_FUZZ'),
  ('TELESCOPING', 'TELESCOPING'), ('TELESCOPED', 'TELESCOPING'), ('TELESCOPE', 'TELESCOPING'),
  ('CORECRUSH', 'CORE_CRUSH'), ('CRUSHEDCORE', 'CORE_CRUSH'), ('COREDAMAGE', 'CORE_CRUSH');

-- The span the data covers, from the file names (one file per source per day).
-- as_of is the end of the last day exported; nothing after it exists yet.
CREATE OR REPLACE TABLE data_window AS
WITH files AS (
  SELECT _file FROM bronze.erp_work_orders
  UNION SELECT _file FROM bronze.coater_runs
  UNION SELECT _file FROM bronze.slitter_runs
  UNION SELECT _file FROM bronze.hmi_downtime
  UNION SELECT _file FROM bronze.qc_defects
  UNION SELECT _file FROM bronze.schedule
),
days AS (
  SELECT TO_DATE(REPLACE(REGEXP_SUBSTR(_file, '[0-9]{4}-?[0-9]{2}-?[0-9]{2}'), '-', ''),
                 'YYYYMMDD') AS file_day
  FROM files
)
SELECT MIN(file_day) AS first_day,
       DATEADD('day', 1, MAX(file_day))::TIMESTAMP_NTZ AS as_of
FROM days;

-- One row per hour of the data window, with the plant's calendar: open Monday 06:00 to
-- Saturday 06:00, three 8-hour shifts starting at 06:00. SEQ4() can skip numbers, so the hour
-- offset comes from ROW_NUMBER().
CREATE OR REPLACE TABLE plant_calendar AS
WITH hours AS (
  SELECT DATEADD('hour', ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1,
                 w.first_day::TIMESTAMP_NTZ) AS hour_local
  FROM TABLE(GENERATOR(ROWCOUNT => 24 * 1000)), data_window w
)
SELECT h.hour_local,
       (DAYOFWEEKISO(h.hour_local) BETWEEN 2 AND 5)
         OR (DAYOFWEEKISO(h.hour_local) = 1 AND HOUR(h.hour_local) >= 6)
         OR (DAYOFWEEKISO(h.hour_local) = 6 AND HOUR(h.hour_local) < 6) AS is_open,
       CASE WHEN HOUR(h.hour_local) BETWEEN 6 AND 13 THEN 'A'
            WHEN HOUR(h.hour_local) BETWEEN 14 AND 21 THEN 'B'
            ELSE 'C' END AS shift
FROM hours h, data_window w
WHERE h.hour_local < w.as_of;

-- Work orders: the ERP exports one row per change, so the latest LAST_MOD is the current state.
CREATE OR REPLACE TABLE work_orders AS
WITH versions AS (
  SELECT wo_num AS wo_id, cust_no AS customer_no, cust_name AS customer_name, item AS sku,
         qty_lf::NUMBER AS qty_ft,
         TO_DATE(due_dt, 'MM/DD/YYYY') AS due_date,
         status,
         TO_TIMESTAMP_NTZ(last_mod, 'YYYY-MM-DD HH24:MI:SS') AS modified_local
  FROM bronze.erp_work_orders
)
SELECT wo_id, customer_no, customer_name, sku, qty_ft, due_date, status,
       MIN(modified_local) OVER (PARTITION BY wo_id) AS released_local,
       MAX(IFF(status = 'COMPLETE', modified_local, NULL)) OVER (PARTITION BY wo_id)
         AS completed_local,
       modified_local AS last_modified_local
FROM versions
QUALIFY ROW_NUMBER() OVER (PARTITION BY wo_id ORDER BY modified_local DESC) = 1;

-- Downtime events from the HMI (JSON). Duplicates share an event_id. The reason is NULL when
-- the operator picked 99 OTHER or nothing; the note is often the only record of what happened.
CREATE OR REPLACE TABLE downtime AS
WITH events AS (
  SELECT v:event_id::STRING AS event_id,
         v:asset::STRING AS asset,
         v:run_ref::STRING AS run_id,
         TO_TIMESTAMP_NTZ(v:ts_start::NUMBER, 3) AS start_utc,  -- epoch milliseconds
         v:duration_s::NUMBER AS duration_s,
         v:reason:code::STRING AS hmi_code,
         v:note::STRING AS note,
         v:operator:badge::STRING AS badge,
         v:operator:shift::STRING AS shift,
         v:hmi_ver::STRING AS hmi_version
  FROM bronze.hmi_downtime
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY _file, _row) = 1
)
SELECT e.event_id,
       ea.equipment,
       e.run_id,
       CONVERT_TIMEZONE('UTC', 'America/New_York', e.start_utc) AS start_local,
       DATEADD('second', e.duration_s, start_local) AS end_local,
       e.start_utc,
       DATEADD('second', e.duration_s, e.start_utc) AS end_utc,
       e.duration_s / 60 AS minutes,
       e.hmi_code,
       r.reason AS hmi_reason,
       e.note,
       UPPER(REPLACE(e.badge, ' ', '')) AS operator,
       e.shift,
       e.hmi_version
FROM events e
LEFT JOIN equipment_alias ea ON ea.alias_key = UPPER(REGEXP_REPLACE(e.asset, '[^A-Za-z0-9]', ''))
LEFT JOIN reasons r ON r.hmi_code = e.hmi_code;

-- Runs from both vendors, in feet and in both time zones. Coater rows carry a UTC offset; a run
-- that crosses midnight appears open (no end) in one file and complete in the next, so the
-- complete row wins. Slitter rows are in meters and local time, finished jobs only. (Local times
-- without an offset are ambiguous for the hour clocks repeat each November; this data ends
-- before then.)
CREATE OR REPLACE TABLE runs AS
WITH coat AS (
  SELECT run_id, work_order AS wo_id, 'coat' AS step, line AS equipment_raw,
         operator AS operator_raw,
         CONVERT_TIMEZONE('America/New_York', start_time::TIMESTAMP_TZ)::TIMESTAMP_NTZ
           AS start_local,
         CONVERT_TIMEZONE('America/New_York', end_time::TIMESTAMP_TZ)::TIMESTAMP_NTZ
           AS end_local,
         good_ft::NUMBER AS good_ft,
         scrap_ft::NUMBER AS scrap_ft
  FROM bronze.coater_runs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY end_time IS NULL, _file DESC) = 1
),
slit AS (
  SELECT job_no, wo, 'slit', machine, operator,
         TO_TIMESTAMP_NTZ(start_local, 'MM/DD/YYYY HH24:MI:SS'),
         TO_TIMESTAMP_NTZ(stop_local, 'MM/DD/YYYY HH24:MI:SS'),
         ROUND(meters_good::FLOAT / 0.3048)::NUMBER,
         ROUND(meters_scrap::FLOAT / 0.3048)::NUMBER
  FROM bronze.slitter_runs
  QUALIFY ROW_NUMBER() OVER (PARTITION BY job_no ORDER BY _file) = 1
),
all_runs AS (
  SELECT * FROM coat
  UNION ALL
  SELECT * FROM slit
),
-- Stops logged against a run while it was on the machine (changeover included; the cleanup
-- after a coating run is logged against the run but happens after it ends).
in_run_stops AS (
  SELECT r.run_id, SUM(d.minutes) AS stop_min
  FROM all_runs r
  JOIN downtime d
    ON d.run_id = r.run_id AND d.start_local >= r.start_local AND d.end_local <= r.end_local
  GROUP BY r.run_id
)
SELECT r.run_id, r.wo_id, r.step, ea.equipment,
       UPPER(REPLACE(r.operator_raw, ' ', '')) AS operator,
       r.start_local, r.end_local,
       CONVERT_TIMEZONE('America/New_York', 'UTC', r.start_local) AS start_utc,
       CONVERT_TIMEZONE('America/New_York', 'UTC', r.end_local) AS end_utc,
       r.end_local IS NOT NULL AS complete,
       r.good_ft + r.scrap_ft AS produced_ft,
       r.good_ft,
       r.scrap_ft,
       DATEDIFF('second', r.start_local, r.end_local) / 60 AS elapsed_min,
       IFF(complete, COALESCE(s.stop_min, 0), NULL) AS downtime_min,
       elapsed_min - downtime_min AS running_min
FROM all_runs r
LEFT JOIN equipment_alias ea
  ON ea.alias_key = UPPER(REGEXP_REPLACE(r.equipment_raw, '[^A-Za-z0-9]', ''))
LEFT JOIN in_run_stops s ON s.run_id = r.run_id;

-- QC defect records, with the defect type mapped to its canonical name.
CREATE OR REPLACE TABLE defects AS
SELECT q.defect_id, q.run_id, m.defect_type, q.defect_type AS defect_type_raw,
       q.footage::NUMBER AS footage_ft,
       TO_TIMESTAMP_NTZ(q.logged_at, 'YYYY-MM-DD HH24:MI') AS logged_local,
       q.inspector
FROM bronze.qc_defects q
LEFT JOIN defect_type_map m
  ON m.spelling_key = UPPER(REGEXP_REPLACE(q.defect_type, '[^A-Za-z]', ''))
QUALIFY ROW_NUMBER() OVER (PARTITION BY q.defect_id ORDER BY q._file, q._row) = 1;

-- The planner's assignments; if a job were ever re-planned, the latest file wins.
CREATE OR REPLACE TABLE schedule AS
SELECT s.wo_id, s.step, ea.equipment,
       TO_TIMESTAMP_NTZ(REPLACE(s.planned_start, 'T', ' '), 'YYYY-MM-DD HH24:MI')
         AS planned_start_local,
       TO_TIMESTAMP_NTZ(REPLACE(s.planned_end, 'T', ' '), 'YYYY-MM-DD HH24:MI')
         AS planned_end_local
FROM bronze.schedule s
LEFT JOIN equipment_alias ea ON ea.alias_key = UPPER(REGEXP_REPLACE(s.machine, '[^A-Za-z0-9]', ''))
QUALIFY ROW_NUMBER() OVER (PARTITION BY s.wo_id, s.step ORDER BY s._file DESC, s._row DESC) = 1;

-- The AI step (04_ai_reasons.sql) writes here. Created once and never replaced: classifying a
-- note costs credits, so each event is classified once and kept.
CREATE TABLE IF NOT EXISTS downtime_ai (
  event_id STRING,
  ai_reason STRING,
  model STRING,
  classified_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
