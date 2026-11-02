#!/usr/bin/env bash
# Upload data/ to the bronze landing stage with the Snowflake CLI (`snow`).
# Needs a configured connection (snow connection add). NOT YET RUN against Snowflake.
#
#   scripts/upload.sh            # all sources, reference and truth
#   CONN=myconn scripts/upload.sh
set -euo pipefail
cd "$(dirname "$0")/.."

conn=(${CONN:+--connection "$CONN"})
stage="@plant.bronze.landing"

put() {  # put <local glob> <stage folder>
  snow sql "${conn[@]}" --role plant_engineer --warehouse plant_wh \
    -q "PUT 'file://$PWD/$1' $stage/$2/ AUTO_COMPRESS = TRUE OVERWRITE = FALSE"
}

for src in erp_work_orders coater_runs slitter_runs hmi_downtime qc_defects schedule; do
  put "data/raw/$src/*" "$src"
done
put "data/reference/*" reference
put "data/truth/*.csv" truth
