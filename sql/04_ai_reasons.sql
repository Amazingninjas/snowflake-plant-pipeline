-- AI step: Cortex reads the note on every stop that hasn't been classified yet and names one
-- reason code. silver.downtime_ai keeps the answers, so each stop is paid for once and a re-run
-- only classifies new stops. A failed call writes nothing, so the next run retries it.
-- Set the model to the winner of 03_ai_compare.sql. The first time, add LIMIT 20 at the
-- marked line and look at the answers before running it on everything.
-- NOT YET RUN against Snowflake.

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;
USE DATABASE plant;
USE SCHEMA silver;

SET model = 'llama3.1-8b';

INSERT INTO downtime_ai (event_id, ai_reason, model)
SELECT event_id, ai_reason, $model
FROM (
  SELECT event_id,
         TRY_PARSE_JSON(AI_COMPLETE(
           model => $model,
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
         )::STRING):reason::STRING AS ai_reason
  FROM (
    SELECT d.event_id,
           REPLACE(REPLACE(p.text, '{machine}', e.kind), '{note}', d.note) AS prompt
    FROM downtime d
    JOIN bronze.ref_equipment e ON e.code = d.equipment
    JOIN bronze.prompts p ON p.name = 'downtime_reason'
    WHERE d.event_id NOT IN (SELECT event_id FROM downtime_ai)
    -- LIMIT 20
  )
)
WHERE ai_reason IN (SELECT reason FROM reasons);

SELECT model, COUNT(*) AS stops, MAX(classified_at) AS last_classified
FROM downtime_ai
GROUP BY model;
