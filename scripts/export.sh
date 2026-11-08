#!/usr/bin/env bash
# Save every silver, gold and eval table from Snowflake as CSV in results/snowflake/: the
# record of the trial run, and what `plantgen emulate --replay` compares itself against.
#
#   read -s -p "key passphrase: " PRIVATE_KEY_PASSPHRASE && export PRIVATE_KEY_PASSPHRASE; echo
#   scripts/export.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${PRIVATE_KEY_PASSPHRASE:-}" ]]; then
  echo "PRIVATE_KEY_PASSPHRASE isn't set in this terminal. Run:" >&2
  echo '  read -s -p "key passphrase: " PRIVATE_KEY_PASSPHRASE && export PRIVATE_KEY_PASSPHRASE; echo' >&2
  exit 1
fi

tables=(
  silver.work_orders silver.runs silver.downtime silver.downtime_ai silver.defects
  silver.schedule silver.plant_calendar silver.data_window silver.equipment_alias
  silver.defect_type_map silver.reasons
  gold.machine_summary gold.downtime_by_reason gold.code_review gold.scrap_pareto
  gold.order_delivery gold.delivery_by_week gold.schedule_adherence gold.daily_machine
  eval.checks eval.reason_accuracy eval.ai_confusion eval.reason_minutes
  eval.ai_trials eval.ai_trial_scores
)

out=results/snowflake
mkdir -p "$out"
for t in "${tables[@]}"; do
  snow sql -c "${CONN:-plant}" --role plant_engineer --warehouse plant_wh --format CSV \
    -q "SELECT * FROM plant.$t" > "$out/$t.tmp"
  # Rows come back in no set order; sorted (no field holds a newline), a re-export of
  # unchanged tables gives the same files
  { head -n 1 "$out/$t.tmp"; tail -n +2 "$out/$t.tmp" | LC_ALL=C sort; } > "$out/$t.csv"
  rm "$out/$t.tmp"
  echo "$t: $(($(wc -l < "$out/$t.csv") - 1)) rows"
done
echo "== saved ${#tables[@]} tables in $out"
