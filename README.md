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
- Default run: 2026-08-03 to 2026-10-29, about 350 work orders, 670 runs and 2,400 downtime
  events. Coater 1 is the bottleneck, about 1 order in 10 ships late, and the hand-built
  schedule drifts.
- About 1,550 of the stops are unplanned, and more than half of those have no usable HMI
  reason code (OTHER or blank). The operator's note is the only record of what happened.
  That is the problem the AI step solves.

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

## Snowflake plan

1. **Setup** (`sql/00_setup.sql`): X-Small warehouse that suspends after 60 s idle, a resource
   monitor that stops it before the trial credits run out, a working role, the `plant`
   database with `bronze`, `silver`, `gold` and `eval` schemas.
2. **Bronze** (`scripts/upload.sh`, then `sql/01_bronze.sql`): files land as-is. Text columns,
   JSON as VARIANT, source file and load time on every row. Re-running COPY loads only new files.
3. **Silver**: one clean row per real thing: latest work-order version, runs merged from both
   vendors in feet and UTC, events flattened from JSON, aliases and spellings mapped, duplicates
   and late rows handled.
4. **Gold**: availability and downtime by machine and reason, scrap Pareto, on-time delivery,
   schedule adherence, output per shift.
5. **AI step**: Cortex classifies each uncoded stop from its note. Accuracy is measured
   against `eval.downtime_truth`, and gold shows downtime by reason before and after.
6. **Check**: gold numbers match `truth/summary.json`.

Silver and gold get written once the trial account exists, so they're tested against
Snowflake itself.

## Trial budget

The trial lasts 30 days or $400 of credits, whichever ends first. Cortex AI functions are
limited to about 1 credit a day until a payment method is added, so the AI step runs in
batches. The whole dataset is about 2 MB, so the warehouse barely runs.

## Status

- [x] Generator and tests
- [x] Setup and bronze SQL written (not yet run against Snowflake)
- [ ] Trial account started
- [ ] Bronze loaded
- [ ] Silver
- [ ] Gold
- [ ] Cortex classification and accuracy check
- [ ] Gold checked against truth
