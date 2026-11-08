#!/usr/bin/env bash
# Run one SQL file on Snowflake and keep the output in logs/<file>.log.
#
#   read -s -p "key passphrase: " PRIVATE_KEY_PASSPHRASE && export PRIVATE_KEY_PASSPHRASE; echo
#   scripts/snowrun.sh sql/00_setup.sql
#   CONN=other scripts/snowrun.sh sql/01_bronze.sql
set -euo pipefail
cd "$(dirname "$0")/.."

file=${1:?usage: scripts/snowrun.sh sql/<file>.sql}
if [[ -z "${PRIVATE_KEY_PASSPHRASE:-}" ]]; then
  echo "PRIVATE_KEY_PASSPHRASE isn't set in this terminal. Run:" >&2
  echo '  read -s -p "key passphrase: " PRIVATE_KEY_PASSPHRASE && export PRIVATE_KEY_PASSPHRASE; echo' >&2
  exit 1
fi

mkdir -p logs
log="logs/$(basename "$file" .sql).log"
echo "== $file  $(date '+%Y-%m-%d %H:%M:%S')" | tee "$log"
snow sql -c "${CONN:-plant}" -f "$file" 2>&1 | tee -a "$log"
echo "== exit ${PIPESTATUS[0]}  (log: $log)" | tee -a "$log"
