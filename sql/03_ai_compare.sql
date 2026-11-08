-- Optional, run once before 04_ai_reasons.sql: try three ways of reading the notes on the same
-- 200 stops, score each against the true reasons, and pick the one 04 uses for every stop.
-- Costs AI credits: 600 calls; on the trial run about 490,000 tokens, 0.58 AI credits ($1.17).
-- Cost and accuracy queries at the bottom. Ran on Snowflake 2026-11-08 (scores in the README).

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;
USE DATABASE plant;
USE SCHEMA eval;

-- 200 stops spread across the window (HASH gives a stable, arbitrary order), with the prompt
-- from prompts/downtime_reason.txt filled in, exactly as 04 builds it.
CREATE OR REPLACE TABLE ai_sample AS
SELECT d.event_id, e.kind, d.note,
       REPLACE(REPLACE(p.text, '{machine}', e.kind), '{note}', d.note) AS prompt
FROM silver.downtime d
JOIN bronze.ref_equipment e ON e.code = d.equipment
JOIN bronze.prompts p ON p.name = 'downtime_reason'
ORDER BY HASH(d.event_id)
LIMIT 200;

CREATE TABLE IF NOT EXISTS ai_trials (
  method STRING,
  event_id STRING,
  ai_reason STRING,
  tried_at TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

-- 1. AI_CLASSIFY: Snowflake picks the model and writes the prompt; we give the labels, with
-- the same descriptions as the prompt file (25 words at most each).
DELETE FROM ai_trials WHERE method = 'ai_classify';
INSERT INTO ai_trials (method, event_id, ai_reason)
SELECT 'ai_classify', event_id,
       AI_CLASSIFY(
         'Machine: ' || kind || '. Note: ' || note,
         [
           {'label': 'WEB_BREAK', 'description': 'the web (the moving sheet of film or foil) broke or snapped and was rethreaded'},
           {'label': 'SPLICE_FAILURE', 'description': 'a splice joining one roll to the next failed, pulled apart or was missed'},
           {'label': 'ADHESIVE_SYSTEM', 'description': 'a problem supplying adhesive: pump, filter, line, tote, air in the line, viscosity. Coaters only'},
           {'label': 'COATING_ADJUST', 'description': 'stopped to fix coating quality on a coater: streaks, lines, coat weight, die gap, deckles, chatter'},
           {'label': 'BLADE_CHANGE', 'description': 'slitter knives or blades were dull, chipped or changed. Slitters only'},
           {'label': 'MATERIAL_SHORTAGE', 'description': 'waiting for material: liner, face stock, cores or a roll that wasn''t staged'},
           {'label': 'QC_HOLD', 'description': 'waiting for QC or the lab to test or approve product'},
           {'label': 'MECHANICAL', 'description': 'a mechanical failure: bearing, belt, chain, shaft, nip roll, tension arm'},
           {'label': 'CONTROLS', 'description': 'an electrical or controls fault: PLC, drive fault, sensor, e-stop, HMI, alarm'},
           {'label': 'STAFFING', 'description': 'no operator available: short-handed, waiting for relief, break coverage, training, pulled to another line'},
           {'label': 'CHANGEOVER', 'description': 'planned setup between orders: product change, new setup, getting ready for the next job'},
           {'label': 'CLEANING', 'description': 'planned cleaning: wash-up, cleaning rolls, die, oven or nips, end-of-run cleanup'},
           {'label': 'PLANNED_MAINTENANCE', 'description': 'scheduled preventive maintenance (PM)'}
         ],
         {'task_description': 'Why did the machine stop? Each note was typed by an operator at a plant that coats adhesive onto film and foil (coaters) and cuts the rolls to width (slitters). Notes are short and informal, with abbreviations and typos.'}
       ):labels[0]::STRING
FROM ai_sample;

-- 2 and 3. AI_COMPLETE with the repo's prompt; the JSON schema limits the answer to the codes.
DELETE FROM ai_trials WHERE method = 'llama3.1-8b';
INSERT INTO ai_trials (method, event_id, ai_reason)
SELECT 'llama3.1-8b', event_id,
       TRY_PARSE_JSON(AI_COMPLETE(
         model => 'llama3.1-8b',
         prompt => prompt,
         model_parameters => {'temperature': 0, 'max_tokens': 20},
         response_format => {'type': 'json', 'schema': {
           'type': 'object',
           'properties': {'reason': {'type': 'string', 'enum': [
             'WEB_BREAK', 'SPLICE_FAILURE', 'ADHESIVE_SYSTEM', 'COATING_ADJUST', 'BLADE_CHANGE',
             'MATERIAL_SHORTAGE', 'QC_HOLD', 'MECHANICAL', 'CONTROLS', 'STAFFING', 'CHANGEOVER',
             'CLEANING', 'PLANNED_MAINTENANCE']}},
           'required': ['reason'],
           'additionalProperties': FALSE}}
       )::STRING):reason::STRING
FROM ai_sample;

DELETE FROM ai_trials WHERE method = 'llama3.3-70b';
INSERT INTO ai_trials (method, event_id, ai_reason)
SELECT 'llama3.3-70b', event_id,
       TRY_PARSE_JSON(AI_COMPLETE(
         model => 'llama3.3-70b',
         prompt => prompt,
         model_parameters => {'temperature': 0, 'max_tokens': 20},
         response_format => {'type': 'json', 'schema': {
           'type': 'object',
           'properties': {'reason': {'type': 'string', 'enum': [
             'WEB_BREAK', 'SPLICE_FAILURE', 'ADHESIVE_SYSTEM', 'COATING_ADJUST', 'BLADE_CHANGE',
             'MATERIAL_SHORTAGE', 'QC_HOLD', 'MECHANICAL', 'CONTROLS', 'STAFFING', 'CHANGEOVER',
             'CLEANING', 'PLANNED_MAINTENANCE']}},
           'required': ['reason'],
           'additionalProperties': FALSE}}
       )::STRING):reason::STRING
FROM ai_sample;

-- Accuracy per method on the sample. "answered" < 200 means some calls failed (NULL).
CREATE OR REPLACE TABLE ai_trial_scores AS
SELECT t.method,
       COUNT(*) AS stops,
       COUNT(t.ai_reason) AS answered,
       COUNT_IF(t.ai_reason = d.reason) AS correct,
       ROUND(100 * COUNT_IF(t.ai_reason = d.reason) / COUNT(*), 1) AS accuracy_pct
FROM ai_trials t
JOIN downtime_truth d ON d.event_id = t.event_id
GROUP BY t.method;

SELECT * FROM ai_trial_scores ORDER BY accuracy_pct DESC;

-- What it cost: run as ACCOUNTADMIN a few hours later (ACCOUNT_USAGE views lag; on the trial
-- run all of it was there four hours after 04). Put your trial's start date in. The first is
-- the total by service, the second tokens and credits per function and model. This run's
-- answers are in results/cost/.
-- USE ROLE ACCOUNTADMIN;
-- SELECT service_type, ROUND(SUM(credits_used), 3) AS credits
-- FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_HISTORY
-- WHERE start_time >= '2026-11-07'
-- GROUP BY service_type ORDER BY credits DESC;
--
-- SELECT function_name, model_name, COUNT(DISTINCT query_id) AS queries,
--        SUM(tokens) AS tokens, ROUND(SUM(token_credits), 3) AS credits
-- FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AISQL_USAGE_HISTORY
-- WHERE usage_time >= '2026-11-07'
-- GROUP BY function_name, model_name
-- ORDER BY credits DESC;
