-- AI step: Cortex reads the note on every stop that hasn't been classified yet and names one
-- reason code. It uses AI_CLASSIFY, the best of the three methods in 03_ai_compare.sql (200 of
-- 200 right on the sample; llama3.3-70b got 195, llama3.1-8b 177). To use a Llama model instead,
-- take the AI_COMPLETE call from 03. silver.downtime_ai keeps the answers, so each stop is paid
-- for once and a re-run only classifies new stops; an answer outside the codes writes nothing,
-- so the next run retries it. To try it small first, remove the -- from the LIMIT line.
-- Cost on the trial run: about 3.9M tokens, 6.36 AI credits (about $12.70) for 2,364 stops.
-- AI_CLASSIFY bills about 1,660 tokens a stop; llama3.3-70b would have cost about 1/16 as much.

USE ROLE plant_engineer;
USE WAREHOUSE plant_wh;
USE DATABASE plant;
USE SCHEMA silver;

INSERT INTO downtime_ai (event_id, ai_reason, model)
SELECT event_id, ai_reason, 'ai_classify'
FROM (
  SELECT event_id,
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
         ):labels[0]::STRING AS ai_reason
  FROM (
    SELECT d.event_id, e.kind, d.note
    FROM downtime d
    JOIN bronze.ref_equipment e ON e.code = d.equipment
    WHERE d.event_id NOT IN (SELECT event_id FROM downtime_ai)
    -- LIMIT 20
  )
)
WHERE ai_reason IN (SELECT reason FROM reasons);

SELECT model, COUNT(*) AS stops, MAX(classified_at) AS last_classified
FROM downtime_ai
GROUP BY model;
