-- One-time setup for a new Snowflake trial account. Run as ACCOUNTADMIN.
-- Written before the trial started; NOT YET RUN against Snowflake.
--
-- Credit discipline: one X-Small warehouse (1 credit per hour while running) that suspends
-- after 60 seconds idle, and a resource monitor that suspends it before the trial runs dry.

USE ROLE ACCOUNTADMIN;

CREATE RESOURCE MONITOR IF NOT EXISTS plant_monitor
  WITH CREDIT_QUOTA = 40              -- ~40 warehouse-hours; raise it if the trial has room
  FREQUENCY = NEVER
  START_TIMESTAMP = IMMEDIATELY
  TRIGGERS
    ON 50 PERCENT DO NOTIFY
    ON 80 PERCENT DO NOTIFY
    ON 100 PERCENT DO SUSPEND;

CREATE WAREHOUSE IF NOT EXISTS plant_wh
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  RESOURCE_MONITOR = plant_monitor;

-- A working role, so nothing day-to-day runs as ACCOUNTADMIN.
CREATE ROLE IF NOT EXISTS plant_engineer;
GRANT ROLE plant_engineer TO ROLE SYSADMIN;
GRANT USAGE, OPERATE ON WAREHOUSE plant_wh TO ROLE plant_engineer;
GRANT CREATE DATABASE ON ACCOUNT TO ROLE plant_engineer;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE plant_engineer;  -- for the AI step
SET me = CURRENT_USER();
GRANT ROLE plant_engineer TO USER IDENTIFIER($me);

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;

CREATE DATABASE IF NOT EXISTS plant;
CREATE SCHEMA IF NOT EXISTS plant.bronze COMMENT = 'Files exactly as the source systems exported them';
CREATE SCHEMA IF NOT EXISTS plant.silver COMMENT = 'Cleaned, typed, deduplicated, one row per real thing';
CREATE SCHEMA IF NOT EXISTS plant.gold   COMMENT = 'Plant metrics for people to read';
CREATE SCHEMA IF NOT EXISTS plant.eval   COMMENT = 'Generator truth, for checking results only';
