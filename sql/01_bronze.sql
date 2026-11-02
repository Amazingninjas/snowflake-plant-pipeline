-- Bronze: land every file as-is. CSV columns stay text, JSON stays VARIANT, and every row
-- keeps the file it came from and when it was loaded. No cleaning here; silver does that.
-- Written before the trial started; NOT YET RUN against Snowflake.
--
-- Before running: upload the files with scripts/upload.sh (or Snowsight: Data > Add data >
-- Load files into a stage), into @plant.bronze.landing/<source>/.
-- Re-running the COPY statements is safe: Snowflake skips files it has already loaded,
-- so new days' files load incrementally.

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;
USE SCHEMA plant.bronze;

CREATE FILE FORMAT IF NOT EXISTS csv_raw
  TYPE = CSV
  SKIP_HEADER = 1
  FIELD_OPTIONALLY_ENCLOSED_BY = '"'
  EMPTY_FIELD_AS_NULL = TRUE
  TRIM_SPACE = FALSE;  -- keep the stray spaces in badge ids; silver deals with them

CREATE FILE FORMAT IF NOT EXISTS json_lines TYPE = JSON;  -- one object per line (or one per file)

CREATE STAGE IF NOT EXISTS landing COMMENT = 'plantgen exports, one folder per source';

-- ERP: one row per work-order change; the latest LAST_MOD is the current state.
CREATE TABLE IF NOT EXISTS erp_work_orders (
  wo_num STRING, cust_no STRING, cust_name STRING, item STRING, qty_lf STRING,
  due_dt STRING, status STRING, last_mod STRING,
  _file STRING, _row NUMBER, _loaded_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
COPY INTO erp_work_orders (wo_num, cust_no, cust_name, item, qty_lf, due_dt, status, last_mod,
                           _file, _row)
  FROM (SELECT $1, $2, $3, $4, $5, $6, $7, $8, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @landing/erp_work_orders/)
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw');

-- Coaters: ISO-8601 with offset; a run crossing midnight appears twice (open, then done).
CREATE TABLE IF NOT EXISTS coater_runs (
  run_id STRING, work_order STRING, line STRING, operator STRING, start_time STRING,
  end_time STRING, good_ft STRING, scrap_ft STRING, avg_fpm STRING,
  _file STRING, _row NUMBER, _loaded_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
COPY INTO coater_runs (run_id, work_order, line, operator, start_time, end_time, good_ft,
                       scrap_ft, avg_fpm, _file, _row)
  FROM (SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @landing/coater_runs/)
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw');

-- Slitters: another vendor. Meters, MM/DD/YYYY HH:MM:SS local time, finished jobs only.
CREATE TABLE IF NOT EXISTS slitter_runs (
  job_no STRING, wo STRING, machine STRING, operator STRING, start_local STRING,
  stop_local STRING, meters_good STRING, meters_scrap STRING, speed_mpm STRING,
  _file STRING, _row NUMBER, _loaded_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
COPY INTO slitter_runs (job_no, wo, machine, operator, start_local, stop_local, meters_good,
                        meters_scrap, speed_mpm, _file, _row)
  FROM (SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @landing/slitter_runs/)
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw');

-- HMI: JSON lines, epoch milliseconds, nested operator; the note is the real information.
CREATE TABLE IF NOT EXISTS hmi_downtime (
  v VARIANT,
  _file STRING, _row NUMBER, _loaded_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
COPY INTO hmi_downtime (v, _file, _row)
  FROM (SELECT $1, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER FROM @landing/hmi_downtime/)
  FILE_FORMAT = (FORMAT_NAME = 'json_lines');

-- QC: defect types spelled several ways.
CREATE TABLE IF NOT EXISTS qc_defects (
  defect_id STRING, run_id STRING, defect_type STRING, footage STRING, logged_at STRING,
  inspector STRING,
  _file STRING, _row NUMBER, _loaded_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
COPY INTO qc_defects (defect_id, run_id, defect_type, footage, logged_at, inspector, _file, _row)
  FROM (SELECT $1, $2, $3, $4, $5, $6, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @landing/qc_defects/)
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw');

-- Planner: each day's new assignments.
CREATE TABLE IF NOT EXISTS schedule (
  wo_id STRING, step STRING, machine STRING, planned_start STRING, planned_end STRING,
  _file STRING, _row NUMBER, _loaded_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
COPY INTO schedule (wo_id, step, machine, planned_start, planned_end, _file, _row)
  FROM (SELECT $1, $2, $3, $4, $5, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER
        FROM @landing/schedule/)
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw');

-- Reference tables (small, replaced on every load). PUT gzips files, hence PATTERN.
CREATE OR REPLACE TABLE ref_products (sku STRING, face_code STRING, face STRING,
  adhesive_code STRING, adhesive STRING, width_in NUMBER, target_fpm NUMBER);
COPY INTO ref_products FROM @landing/reference/ PATTERN = '.*products[.]csv.*'
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw') FORCE = TRUE;

CREATE OR REPLACE TABLE ref_equipment (code STRING, kind STRING, max_width_in NUMBER,
  aliases STRING);
COPY INTO ref_equipment FROM @landing/reference/ PATTERN = '.*equipment[.]csv.*'
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw') FORCE = TRUE;

CREATE OR REPLACE TABLE ref_operators (badge STRING, area STRING, shift STRING);
COPY INTO ref_operators FROM @landing/reference/ PATTERN = '.*operators[.]csv.*'
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw') FORCE = TRUE;

CREATE OR REPLACE TABLE ref_downtime_codes (hmi_code STRING, hmi_text STRING, reason STRING,
  planned BOOLEAN);
COPY INTO ref_downtime_codes FROM @landing/reference/ PATTERN = '.*downtime_codes[.]csv.*'
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw') FORCE = TRUE;

-- Prompts for the AI step live in the repo (prompts/*.txt) and load like any other file:
-- no delimiters, so each file arrives whole as one row.
CREATE FILE FORMAT IF NOT EXISTS whole_file TYPE = CSV FIELD_DELIMITER = NONE RECORD_DELIMITER = NONE;
CREATE OR REPLACE TABLE prompts (name STRING, text STRING);
COPY INTO prompts (name, text)
  FROM (SELECT REPLACE(SPLIT_PART(METADATA$FILENAME, '/', -1), '.txt.gz', ''), $1
        FROM @landing/prompts/)
  FILE_FORMAT = (FORMAT_NAME = 'whole_file') FORCE = TRUE;

-- Truth, into the eval schema: only for checking silver/gold and the AI step.
CREATE OR REPLACE TABLE plant.eval.downtime_truth (event_id STRING, equipment STRING,
  run_id STRING, reason STRING, planned BOOLEAN, start_local TIMESTAMP_NTZ,
  end_local TIMESTAMP_NTZ, minutes FLOAT);
COPY INTO plant.eval.downtime_truth FROM @landing/truth/ PATTERN = '.*downtime_truth[.]csv.*'
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw') FORCE = TRUE;

CREATE OR REPLACE TABLE plant.eval.runs_truth (run_id STRING, wo_id STRING, step STRING,
  equipment STRING, operator STRING, start_local TIMESTAMP_NTZ, end_local TIMESTAMP_NTZ,
  produced_ft NUMBER, good_ft NUMBER, scrap_ft NUMBER, running_min FLOAT, complete BOOLEAN);
COPY INTO plant.eval.runs_truth FROM @landing/truth/ PATTERN = '.*runs_truth[.]csv.*'
  FILE_FORMAT = (FORMAT_NAME = 'csv_raw') FORCE = TRUE;

CREATE OR REPLACE TABLE plant.eval.summary (v VARIANT);
COPY INTO plant.eval.summary FROM @landing/truth/ PATTERN = '.*summary[.]json.*'
  FILE_FORMAT = (FORMAT_NAME = 'json_lines') FORCE = TRUE;

-- Quick check: rows per source and file.
SELECT 'erp_work_orders' AS source, COUNT(*) AS rows_loaded, COUNT(DISTINCT _file) AS files
  FROM erp_work_orders
UNION ALL SELECT 'coater_runs', COUNT(*), COUNT(DISTINCT _file) FROM coater_runs
UNION ALL SELECT 'slitter_runs', COUNT(*), COUNT(DISTINCT _file) FROM slitter_runs
UNION ALL SELECT 'hmi_downtime', COUNT(*), COUNT(DISTINCT _file) FROM hmi_downtime
UNION ALL SELECT 'qc_defects', COUNT(*), COUNT(DISTINCT _file) FROM qc_defects
UNION ALL SELECT 'schedule', COUNT(*), COUNT(DISTINCT _file) FROM schedule;
