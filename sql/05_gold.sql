-- Gold: plant metrics for people to read, rebuilt from silver on every run.
-- Run after 02_silver.sql, and after 04_ai_reasons.sql when there are new stops to classify.
-- Tested on the local emulator (plantgen emulate); NOT YET RUN against Snowflake.

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;
USE DATABASE plant;
USE SCHEMA gold;

-- Per machine over the whole window. Running time counts finished runs; downtime counts every
-- stop that had ended. Availability = running / (running + downtime): of the time a machine
-- was busy with jobs, PM included, how much it actually ran. Utilization = running / open.
CREATE OR REPLACE TABLE machine_summary AS
WITH open_time AS (
  SELECT COUNT_IF(is_open) AS open_hours FROM silver.plant_calendar
),
finished AS (
  SELECT equipment, COUNT(*) AS runs, SUM(running_min) / 60 AS running_hours,
         SUM(produced_ft) AS produced_ft, SUM(good_ft) AS good_ft, SUM(scrap_ft) AS scrap_ft
  FROM silver.runs
  WHERE complete
  GROUP BY equipment
),
stops AS (
  SELECT equipment, COUNT(*) AS downtime_events, SUM(minutes) / 60 AS downtime_hours
  FROM silver.downtime
  GROUP BY equipment
)
SELECT f.equipment AS machine,
       e.kind,
       o.open_hours,
       f.runs,
       ROUND(f.running_hours, 2) AS running_hours,
       s.downtime_events,
       ROUND(s.downtime_hours, 2) AS downtime_hours,
       f.produced_ft,
       f.good_ft,
       f.scrap_ft,
       ROUND(100 * f.scrap_ft / f.produced_ft, 2) AS scrap_pct,
       ROUND(100 * f.running_hours / o.open_hours, 1) AS utilization_pct,
       ROUND(100 * f.running_hours / (f.running_hours + s.downtime_hours), 1) AS availability_pct
FROM finished f
JOIN stops s ON s.equipment = f.equipment
JOIN bronze.ref_equipment e ON e.code = f.equipment
CROSS JOIN open_time o;

-- Downtime by machine and reason, two ways: from the HMI codes alone (99 OTHER and blanks
-- become UNKNOWN), and with the AI's reading of the note filling in what the HMI code lacks.
CREATE OR REPLACE TABLE downtime_by_reason AS
WITH stops AS (
  SELECT d.equipment, d.minutes,
         COALESCE(d.hmi_reason, 'UNKNOWN') AS hmi_reason,
         COALESCE(d.hmi_reason, a.ai_reason, 'UNKNOWN') AS reason
  FROM silver.downtime d
  LEFT JOIN silver.downtime_ai a ON a.event_id = d.event_id
),
with_ai AS (
  SELECT equipment, reason, COUNT(*) AS events, SUM(minutes) AS minutes
  FROM stops GROUP BY equipment, reason
),
hmi_only AS (
  SELECT equipment, hmi_reason AS reason, COUNT(*) AS events, SUM(minutes) AS minutes
  FROM stops GROUP BY equipment, hmi_reason
)
SELECT COALESCE(w.equipment, h.equipment) AS machine,
       COALESCE(w.reason, h.reason) AS reason,
       r.planned,
       COALESCE(w.events, 0) AS events,
       ROUND(COALESCE(w.minutes, 0), 1) AS minutes,
       COALESCE(h.events, 0) AS hmi_only_events,
       ROUND(COALESCE(h.minutes, 0), 1) AS hmi_only_minutes
FROM with_ai w
FULL OUTER JOIN hmi_only h ON h.equipment = w.equipment AND h.reason = w.reason
LEFT JOIN silver.reasons r ON r.reason = COALESCE(w.reason, h.reason);

-- Scrap Pareto: QC footage by defect type, biggest first, with the running share.
CREATE OR REPLACE TABLE scrap_pareto AS
SELECT COALESCE(defect_type, 'UNMAPPED') AS defect_type,
       COUNT(*) AS records,
       SUM(footage_ft) AS footage_ft,
       ROUND(100 * SUM(footage_ft) / SUM(SUM(footage_ft)) OVER (), 1) AS pct,
       ROUND(100 * SUM(SUM(footage_ft)) OVER (ORDER BY SUM(footage_ft) DESC
                                                ROWS UNBOUNDED PRECEDING)
             / SUM(SUM(footage_ft)) OVER (), 1) AS cumulative_pct
FROM silver.defects
GROUP BY COALESCE(defect_type, 'UNMAPPED');

-- One row per completed order. On time = finished on or before the due date (plant days).
CREATE OR REPLACE TABLE order_delivery AS
SELECT wo_id, customer_name, sku, qty_ft, due_date,
       completed_local::DATE AS completed_date,
       GREATEST(DATEDIFF('day', due_date, completed_local::DATE), 0) AS days_late,
       days_late = 0 AS on_time
FROM silver.work_orders
WHERE completed_local IS NOT NULL;

CREATE OR REPLACE TABLE delivery_by_week AS
SELECT DATE_TRUNC('week', completed_date) AS week_starting,
       COUNT(*) AS completed,
       COUNT_IF(on_time) AS on_time,
       ROUND(100 * COUNT_IF(on_time) / COUNT(*), 1) AS on_time_pct,
       MAX(days_late) AS max_days_late
FROM order_delivery
GROUP BY DATE_TRUNC('week', completed_date);

-- How far actual starts drift from the hand-built schedule, per machine.
CREATE OR REPLACE TABLE schedule_adherence AS
WITH jobs AS (
  SELECT s.equipment, DATEDIFF('second', s.planned_start_local, r.start_local) / 60 AS delay_min
  FROM silver.schedule s
  JOIN silver.runs r ON r.wo_id = s.wo_id AND r.step = s.step
)
SELECT equipment AS machine,
       COUNT(*) AS jobs,
       ROUND(AVG(delay_min) / 60, 2) AS avg_start_delay_hours,
       ROUND(100 * COUNT_IF(ABS(delay_min) <= 120) / COUNT(*), 1) AS within_2h_pct,
       ROUND(MAX(delay_min) / 60, 1) AS max_start_delay_hours
FROM jobs
GROUP BY equipment;

-- Daily output and downtime per machine, for trend charts. Output counts on the day a run
-- finished; downtime on the day a stop started.
CREATE OR REPLACE TABLE daily_machine AS
WITH output AS (
  SELECT end_local::DATE AS day, equipment, COUNT(*) AS runs_finished, SUM(good_ft) AS good_ft
  FROM silver.runs WHERE complete GROUP BY 1, 2
),
stops AS (
  SELECT start_local::DATE AS day, equipment, COUNT(*) AS stops, SUM(minutes) AS downtime_min
  FROM silver.downtime GROUP BY 1, 2
)
SELECT COALESCE(o.day, s.day) AS day,
       COALESCE(o.equipment, s.equipment) AS machine,
       COALESCE(o.runs_finished, 0) AS runs_finished,
       COALESCE(o.good_ft, 0) AS good_ft,
       COALESCE(s.stops, 0) AS stops,
       ROUND(COALESCE(s.downtime_min, 0), 1) AS downtime_min
FROM output o
FULL OUTER JOIN stops s ON s.day = o.day AND s.equipment = o.equipment;
