# Plant floor data pipeline on Snowflake

A bronze → silver → gold ("medallion") pipeline for a coating and slitting plant, built on
Snowflake, with an AI step: Snowflake Cortex reads operators' free-text downtime notes and
assigns each stop a reason code.

The plant is synthetic. `plantgen` simulates it and writes the files its systems would export,
including the mess real exports have. Nothing here comes from a real company. Because the
generator also writes the true answers, every number the pipeline produces can be checked.

## The plant

- **Coater 1** (60" web) and **Coater 2** (30" web, older and slower) coat adhesive and
  laminate master rolls. **Slitter 1** and **Slitter 2** cut them to width.
- 12 products (polyester, polypropylene, polyimide and foil faces; acrylic, hot-melt and
  silicone adhesives). Runs 24/5, Monday 06:00 to Saturday 06:00, three shifts.
- Default run: 2026-08-03 to 2026-10-29, 352 work orders, 670 runs and 2,364 downtime events.
  Coater 1 is the bottleneck, about 1 order in 10 ships late, and jobs start about a day after
  the hand-built schedule says they will.
- 1,549 of the stops are unplanned, and more than half of those have no usable HMI reason code
  (OTHER or blank). The operator's note is the only record of what happened. That is the
  problem the AI step solves.
- The notes come from a fixed set of phrasings per reason, roughed up with abbreviations,
  typos and shouting, and I wrote the reason descriptions in the prompt knowing those
  phrasings. Real notes are messier, so read the AI accuracy here as a best case.

## Sources and their quirks

| Source | Format | What silver has to deal with |
|---|---|---|
| `erp_work_orders` | CSV, one row per change | latest `LAST_MOD` wins; cancellations; MM/DD/YYYY dates |
| `coater_runs` | CSV, ISO-8601 with UTC offset | a run crossing midnight appears open (blank end), then complete |
| `slitter_runs` | CSV from another vendor | meters, local MM/DD/YYYY HH:MM:SS, different headers |
| `hmi_downtime` | JSON lines | epoch milliseconds, nested operator, reason usually OTHER or missing |
| `qc_defects` | CSV | defect types spelled several ways |
| `schedule` | CSV | planned start/end per job (compare with actuals) |
| all of them | | equipment spelled several ways, sloppy badge ids, ~1% duplicate rows, ~3% of rows in the next day's file |

`data/reference/` has the lookup tables. `data/truth/` has the answers (true downtime reasons,
true run results, a summary); it goes into a separate `eval` schema, never into bronze.

## Generate the data

```bash
uv sync
uv run plantgen generate                 # writes data/raw, data/reference, data/truth
uv run plantgen generate --days 30 --seed 3 --out /tmp/plant
uv run plantgen generate --clean         # no quirks, for comparison
uv run pytest
```

## Run it locally first

`plantgen emulate` runs the pipeline's SQL on [fakesnow](https://github.com/tekumara/fakesnow), a
Snowflake emulator built on DuckDB, and checks gold against the truth. The emulator can't read
files from a stage, so it loads bronze itself (same tables, same `_file` and `_row` columns);
silver, gold and the checks run from `sql/` as written.

```bash
uv run plantgen emulate                  # bronze, silver, gold, 80+ checks against truth
uv run plantgen emulate --classify http://127.0.0.1:11439 --sample 200   # AI step on a local model
```

`--classify` stands in for Cortex with any OpenAI-compatible server: same prompt file, and a
JSON schema that only allows the reason codes. The tests run the whole pipeline on messy and
clean data. Passing locally means the logic is right; the run on Snowflake is still the one
that counts.

Local run on all 2,364 stops (Flash-Next, a local model, 20 notes per request, about 90
minutes), all 82 checks passing:

| Reason from | Unplanned stops right | Planned stops right |
|---|---|---|
| HMI code, where the operator picked one | 638 of 717 (89.0%) | 740 of 740 (100%) |
| HMI code, all stops (blank or OTHER counts as wrong) | 638 of 1,549 (41.2%) | 740 of 815 (90.8%) |
| HMI code, else AI | 1,470 of 1,549 (94.9%) | 815 of 815 (100%) |
| AI on every stop | 1,549 of 1,549 (100%) | 812 of 815 (99.6%) |

The AI's three misses were "cleanup and changeover" (twice, called CLEANING) and "iwpe down
nips" (called MECHANICAL). Where the operator's code and the AI disagreed (82 stops), the AI
was right 79 times, so trusting the code first loses about 5 points on unplanned stops. On
real notes I'd expect lower AI numbers (see the caveat above), and stops where the two
disagree are the ones worth a person's look.

## Snowflake run order

| File | What it does |
|---|---|
| `sql/00_setup.sql` | X-Small warehouse that suspends after 60 s idle, a resource monitor that stops it before the trial credits run out, a working role, the `plant` database with `bronze`, `silver`, `gold` and `eval` schemas |
| `scripts/upload.sh` | PUTs `data/` and `prompts/` into the landing stage (Snowflake CLI) |
| `sql/01_bronze.sql` | Files land as-is: text columns, JSON as VARIANT, source file and load time on every row. Re-running COPY loads only new files |
| `sql/02_silver.sql` | One clean row per real thing: latest work-order version, runs merged from both vendors in feet and UTC, events flattened from JSON, aliases and spellings mapped, duplicates and late rows handled, running time = elapsed minus logged stops |
| `sql/03_ai_compare.sql` | Optional, once: AI_CLASSIFY, llama3.1-8b and llama3.3-70b on the same 200 stops, scored against the truth |
| `sql/04_ai_reasons.sql` | Cortex reads every stop's note that hasn't been classified yet and names a reason code; answers are kept, so re-runs only pay for new stops |
| `sql/05_gold.sql` | Machine summary (availability, utilization, scrap), downtime by reason before and after AI, scrap Pareto, on-time delivery by order and week, schedule adherence, daily output |
| `sql/06_check.sql` | Every gold number against `truth/summary.json`, data-quality counts, and the AI's accuracy per stop and per minute |

## Trial budget

The trial lasts 30 days or $400 of credits, whichever ends first. The whole dataset is about
2 MB, so the warehouse barely runs. Cortex is billed in AI credits per million tokens
([Service Consumption Table](https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf),
effective 2026-10-29, Table 6(a)); one AI credit is $2.00 with cross-region routing, $2.20
without ([pricing](https://docs.snowflake.com/en/user-guide/snowflake-cortex/pricing)).

| Step | Tokens (my estimate) | Rate (AI credits per 1M) | Cost |
|---|---|---|---|
| 04 on all 2,364 stops, llama3.1-8b | ~1.0M (≈410 per stop) | 0.132 | ~0.13 credits, ~$0.30 |
| 04 on all 2,364 stops, llama3.3-70b | ~1.0M | 0.432 | ~0.42 credits, ~$0.90 |
| 03 comparison, 200 stops × 3 methods | ~0.2M | 0.132 / 0.432 / 1.62 (AI_CLASSIFY) | ~0.25 credits, ~$0.50 |

Token counts are estimates from the prompt length; `03_ai_compare.sql` ends with the query
that shows what was actually billed. Without a payment method, Cortex use on a trial is
limited (about 1 credit a day, as I understand it), which these steps fit inside.

## Status

- [x] Generator and tests
- [x] Setup, bronze, silver, gold, AI and check SQL written
- [x] Silver, gold and checks pass on the local emulator, messy and clean data
- [x] AI step tried locally (Flash-Next, a local model)
- [ ] Trial account started
- [ ] Bronze loaded
- [ ] Silver, gold and checks on Snowflake
- [ ] Cortex comparison and classification
- [ ] Results and screenshots in the repo
