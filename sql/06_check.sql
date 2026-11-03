-- Check: compare gold with the generator's answers (eval schema), and measure the AI step.
-- Run after 05_gold.sql. Every row of eval.checks should pass; the last query lists failures.
-- Tested on the local emulator (plantgen emulate); NOT YET RUN against Snowflake.

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;
USE DATABASE plant;
USE SCHEMA eval;

CREATE OR REPLACE TABLE checks AS
WITH truth AS (
  SELECT e.key AS machine, e.value AS t
  FROM summary s, LATERAL FLATTEN(input => s.v:equipment) e
),
m AS (
  SELECT t.machine, t.t, g.open_hours, g.runs, g.running_hours, g.produced_ft, g.good_ft,
         g.scrap_ft, g.downtime_events, g.downtime_hours
  FROM truth t
  LEFT JOIN gold.machine_summary g ON g.machine = t.machine
),
d AS (
  SELECT s.v:delivery AS t, o.*
  FROM summary s,
       (SELECT COUNT(*) AS completed,
               COUNT_IF(on_time) AS on_time,
               ROUND(100 * COUNT_IF(on_time) / COUNT(*), 1) AS on_time_pct,
               ROUND(AVG(NULLIF(days_late, 0)), 2) AS avg_days_late,
               MAX(days_late) AS max_days_late
        FROM gold.order_delivery) o
),
scrap AS (
  SELECT e.key AS defect_type, e.value AS t, p.records, p.footage_ft
  FROM summary s, LATERAL FLATTEN(input => s.v:scrap_by_defect) e
  LEFT JOIN gold.scrap_pareto p ON p.defect_type = e.key
),
sched AS (
  SELECT e.key AS machine, e.value AS t, a.jobs, a.avg_start_delay_hours, a.within_2h_pct
  FROM summary s, LATERAL FLATTEN(input => s.v:schedule) e
  LEFT JOIN gold.schedule_adherence a ON a.machine = e.key
),
pairs AS (
  SELECT 'machine' AS area, machine AS item, 'runs' AS metric, t:runs::FLOAT AS expected,
         runs AS actual, 0 AS tolerance FROM m
  UNION ALL SELECT 'machine', machine, 'running_hours', t:running_hours::FLOAT, running_hours, 0.05 FROM m
  UNION ALL SELECT 'machine', machine, 'produced_ft', t:produced_ft::FLOAT, produced_ft, 0 FROM m
  UNION ALL SELECT 'machine', machine, 'good_ft', t:good_ft::FLOAT, good_ft, 0 FROM m
  UNION ALL SELECT 'machine', machine, 'scrap_ft', t:scrap_ft::FLOAT, scrap_ft, 0 FROM m
  UNION ALL SELECT 'machine', machine, 'downtime_events', t:downtime_events::FLOAT, downtime_events, 0 FROM m
  UNION ALL SELECT 'machine', machine, 'downtime_min', t:downtime_min::FLOAT, downtime_hours * 60, 0.5 FROM m
  UNION ALL SELECT 'machine', machine, 'open_hours', s.v:open_hours_per_machine::FLOAT, open_hours, 0
            FROM m, summary s
  UNION ALL SELECT 'delivery', 'all', 'completed', t:completed::FLOAT, completed, 0 FROM d
  UNION ALL SELECT 'delivery', 'all', 'on_time', t:on_time::FLOAT, on_time, 0 FROM d
  UNION ALL SELECT 'delivery', 'all', 'on_time_pct', t:on_time_pct::FLOAT, on_time_pct, 0.05 FROM d
  UNION ALL SELECT 'delivery', 'all', 'avg_days_late', t:avg_days_late::FLOAT, COALESCE(avg_days_late, 0), 0.005 FROM d
  UNION ALL SELECT 'delivery', 'all', 'max_days_late', t:max_days_late::FLOAT, max_days_late, 0 FROM d
  UNION ALL SELECT 'scrap', defect_type, 'records', t:records::FLOAT, records, 0 FROM scrap
  UNION ALL SELECT 'scrap', defect_type, 'footage_ft', t:footage::FLOAT, footage_ft, 0 FROM scrap
  UNION ALL SELECT 'schedule', machine, 'jobs', t:jobs::FLOAT, jobs, 0 FROM sched
  UNION ALL SELECT 'schedule', machine, 'avg_start_delay_hours', t:avg_start_delay_hours::FLOAT, avg_start_delay_hours, 0.005 FROM sched
  UNION ALL SELECT 'schedule', machine, 'within_2h_pct', t:within_2h_pct::FLOAT, within_2h_pct, 0.05 FROM sched
  UNION ALL SELECT 'orders', 'all', 'work_orders', s.v:work_orders::FLOAT,
                   (SELECT COUNT(*) FROM silver.work_orders), 0 FROM summary s
  UNION ALL SELECT 'orders', 'all', 'cancelled', s.v:cancelled::FLOAT,
                   (SELECT COUNT_IF(status = 'CANCELLED') FROM silver.work_orders), 0 FROM summary s
  -- Data quality: everything a source spelled oddly should have found its match.
  UNION ALL SELECT 'quality', 'downtime', 'unmatched equipment', 0, COUNT_IF(equipment IS NULL), 0 FROM silver.downtime
  UNION ALL SELECT 'quality', 'runs', 'unmatched equipment', 0, COUNT_IF(equipment IS NULL), 0 FROM silver.runs
  UNION ALL SELECT 'quality', 'schedule', 'unmatched equipment', 0, COUNT_IF(equipment IS NULL), 0 FROM silver.schedule
  UNION ALL SELECT 'quality', 'defects', 'unmapped defect type', 0, COUNT_IF(defect_type IS NULL), 0 FROM silver.defects
  UNION ALL SELECT 'quality', 'runs', 'unknown operator', 0, COUNT(*), 0
            FROM silver.runs r LEFT JOIN bronze.ref_operators o ON o.badge = r.operator WHERE o.badge IS NULL
  UNION ALL SELECT 'quality', 'downtime', 'unknown operator', 0, COUNT(*), 0
            FROM silver.downtime d LEFT JOIN bronze.ref_operators o ON o.badge = d.operator WHERE o.badge IS NULL
  UNION ALL SELECT 'quality', 'runs', 'order not in ERP', 0, COUNT(*), 0
            FROM silver.runs r LEFT JOIN silver.work_orders w ON w.wo_id = r.wo_id WHERE w.wo_id IS NULL
  UNION ALL SELECT 'quality', 'runs', 'duplicate run_id', 0, COUNT(*) - COUNT(DISTINCT run_id), 0 FROM silver.runs
  UNION ALL SELECT 'quality', 'downtime', 'duplicate event_id', 0, COUNT(*) - COUNT(DISTINCT event_id), 0 FROM silver.downtime
)
SELECT area, item, metric, expected, actual,
       COALESCE(ABS(expected - actual) <= tolerance, FALSE) AS pass
FROM pairs;

-- How well each way of assigning a reason matches the true reason, per stop and per minute.
-- "coded stops only" counts stops the operator coded (not 99 OTHER, not blank); "classified
-- stops" counts stops the AI has answered for; "all stops" counts every stop, and a stop with
-- no reason counts as wrong.
CREATE OR REPLACE TABLE reason_accuracy AS
WITH stops AS (
  SELECT t.reason AS true_reason, t.planned, d.minutes, d.hmi_reason, a.ai_reason,
         COALESCE(d.hmi_reason, a.ai_reason) AS final_reason
  FROM silver.downtime d
  JOIN downtime_truth t ON t.event_id = d.event_id
  LEFT JOIN silver.downtime_ai a ON a.event_id = d.event_id
),
scored AS (
  SELECT 'hmi code, coded stops only' AS method, planned, minutes,
         hmi_reason = true_reason AS correct
  FROM stops WHERE hmi_reason IS NOT NULL
  UNION ALL
  SELECT 'hmi code, all stops', planned, minutes, COALESCE(hmi_reason = true_reason, FALSE)
  FROM stops
  UNION ALL
  SELECT 'ai, classified stops', planned, minutes, ai_reason = true_reason
  FROM stops WHERE ai_reason IS NOT NULL
  UNION ALL
  SELECT 'hmi code, else ai, all stops', planned, minutes, COALESCE(final_reason = true_reason, FALSE)
  FROM stops
  UNION ALL
  -- the stops in gold.code_review, scored both ways
  SELECT 'code review stops, hmi code', planned, minutes, hmi_reason = true_reason
  FROM stops WHERE hmi_reason <> ai_reason
  UNION ALL
  SELECT 'code review stops, ai', planned, minutes, ai_reason = true_reason
  FROM stops WHERE hmi_reason <> ai_reason
)
SELECT method,
       IFF(planned, 'planned', 'unplanned') AS stop_type,
       COUNT(*) AS stops,
       COUNT_IF(correct) AS correct,
       ROUND(100 * COUNT_IF(correct) / COUNT(*), 1) AS accuracy_pct,
       ROUND(100 * SUM(IFF(correct, minutes, 0)) / SUM(minutes), 1) AS minutes_right_pct
FROM scored
GROUP BY method, planned;

-- Where the AI goes wrong: true reason against the AI's answer.
CREATE OR REPLACE TABLE ai_confusion AS
SELECT t.reason AS true_reason, COALESCE(a.ai_reason, '(none)') AS ai_reason, COUNT(*) AS stops
FROM silver.downtime d
JOIN downtime_truth t ON t.event_id = d.event_id
LEFT JOIN silver.downtime_ai a ON a.event_id = d.event_id
GROUP BY t.reason, COALESCE(a.ai_reason, '(none)');

-- Downtime minutes by true reason against what gold reports, per machine.
CREATE OR REPLACE TABLE reason_minutes AS
WITH truth AS (
  SELECT equipment AS machine, reason, SUM(minutes) AS true_minutes
  FROM downtime_truth GROUP BY equipment, reason
)
SELECT COALESCE(t.machine, g.machine) AS machine,
       COALESCE(t.reason, g.reason) AS reason,
       ROUND(COALESCE(t.true_minutes, 0), 1) AS true_minutes,
       COALESCE(g.minutes, 0) AS gold_minutes,
       COALESCE(g.hmi_only_minutes, 0) AS hmi_only_minutes
FROM truth t
FULL OUTER JOIN gold.downtime_by_reason g ON g.machine = t.machine AND g.reason = t.reason;

SELECT area, item, metric, expected, actual FROM checks WHERE NOT pass ORDER BY area, item, metric;
