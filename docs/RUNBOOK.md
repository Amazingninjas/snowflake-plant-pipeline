# Trial runbook

Every step of the Snowflake run, in order, with what each one should show. The 30-day clock
starts at signup, so do part A first; it needs no account. Run commands from the repo root.
Expected counts are from the local emulator on the default data (`plantgen generate`, 88
days, seed 7).

## A. Before signup

1. Make the data and check the pipeline locally:

   ```bash
   uv run plantgen generate        # writes data/ (same files every time for the same seed)
   uv run plantgen emulate         # should end: checks: 82 passed, 0 failed
   ```

2. Make a key pair for the CLI. Snowsight accounts need MFA, and a key pair lets `snow` log
   in without a code each time. `openssl` asks for a passphrase; pick one and keep it in your
   password manager.

   ```bash
   mkdir -p ~/.snowflake/keys && chmod 700 ~/.snowflake ~/.snowflake/keys
   cd ~/.snowflake/keys
   openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 des3 -inform PEM -out rsa_key.p8
   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
   chmod 600 rsa_key.p8
   cd -
   ```

   `rsa_key.p8` is the secret half and never leaves this machine. `rsa_key.pub` is the
   public half and goes into Snowflake in B3.

3. `snow --version` should print 3.32 or later. If it's missing: `uv tool install snowflake-cli`.

## B. Sign up and connect

1. Sign up at [signup.snowflake.com](https://signup.snowflake.com/) with a personal email, on
   your own time and machine. Edition: Enterprise (the default). Cloud: **AWS**. Region:
   **US East (N. Virginia)** or **US West (Oregon)**, where llama3.1-8b and llama3.3-70b run
   without cross-region calls.
2. Activate from the email, choose a username and password, and enroll MFA when Snowsight
   asks.
3. In a Snowsight worksheet (Projects > Workspaces, or Worksheets):

   ```sql
   SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS account, CURRENT_USER();
   ```

   Then give your user the public key. This prints the statement to paste (the public key
   isn't secret):

   ```bash
   echo "ALTER USER <your user> SET RSA_PUBLIC_KEY='$(grep -v 'PUBLIC KEY' ~/.snowflake/keys/rsa_key.pub | tr -d '\n')';"
   ```

   Run it in the worksheet, then `DESC USER <your user>;`. The `RSA_PUBLIC_KEY_FP` row
   should match:

   ```bash
   openssl rsa -pubin -in ~/.snowflake/keys/rsa_key.pub -outform DER | openssl dgst -sha256 -binary | openssl enc -base64
   ```

4. Add the connection and test it. `read -s` keeps the passphrase off the screen and out of
   shell history; it lasts until the terminal closes.

   ```bash
   snow connection add --connection-name plant --account <ORG-ACCOUNT from B3> \
     --user <your user> --authenticator SNOWFLAKE_JWT \
     --private-key ~/.snowflake/keys/rsa_key.p8 --default --no-interactive
   read -s -p "key passphrase: " PRIVATE_KEY_PASSPHRASE && export PRIVATE_KEY_PASSPHRASE; echo
   snow connection test -c plant
   ```

   Expect a table ending in `Status | OK`.

## C. Load and build (no AI yet)

| Step | Command | What you should see |
|---|---|---|
| 1. Setup | `snow sql -c plant -f sql/00_setup.sql` | Each statement succeeds. It runs as ACCOUNTADMIN and makes the warehouse, monitor, role, database, four schemas and the landing stage |
| 2. Upload | `CONN=plant scripts/upload.sh` | One PUT per folder, every file `UPLOADED`. The count below should say 458 |
| 3. Bronze | `snow sql -c plant -f sql/01_bronze.sql` | Each COPY reports files `LOADED` and no errors |
| 4. Silver | `snow sql -c plant -f sql/02_silver.sql` | Each table created |
| 5. Gold | `snow sql -c plant -f sql/05_gold.sql` | Each table created |
| 6. Check | `snow sql -c plant -f sql/06_check.sql` | The last query returns **no rows** (no failed checks) |

Count the uploaded files after step 2:

```bash
snow sql -c plant -q "LIST @plant.bronze.landing; SELECT COUNT(*) FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))"
```

458 = 450 raw (coater 77, ERP 77, HMI 78, QC 80, schedule 63, slitter 75), 4 reference, 3
truth and 1 prompt. With other generate settings, expect `find data -type f | wc -l` plus 1.

Row counts to compare after step 6 (run in a worksheet):

```sql
SELECT table_schema, table_name, row_count
FROM plant.information_schema.tables
WHERE table_schema IN ('BRONZE', 'SILVER', 'GOLD', 'EVAL')
ORDER BY 1, 2;
```

| Table | Rows | | Table | Rows |
|---|---|---|---|---|
| bronze.erp_work_orders | 1,433 | | silver.work_orders | 352 |
| bronze.coater_runs | 448 | | silver.runs | 672 |
| bronze.slitter_runs | 338 | | silver.downtime | 2,364 |
| bronze.hmi_downtime | 2,379 | | silver.defects | 1,630 |
| bronze.qc_defects | 1,647 | | silver.schedule | 698 |
| bronze.schedule | 698 | | silver.plant_calendar | 2,112 |
| bronze.prompts | 1 | | gold.order_delivery | 333 |
| eval.checks | 82 | | gold.machine_summary | 4 |

Bronze has more rows than silver on purpose: ERP keeps every version of an order, the
coater export repeats runs that were still open, and the HMI feed has duplicate events.

**Screenshot:** the check query with no rows, and `SELECT * FROM plant.gold.machine_summary`.

## D. The AI step

The resource monitor from setup only stops the warehouse. Cortex is billed as AI credits,
which it doesn't cap, so the guard here is running small first.

1. Compare models on 200 stops (about 600 calls; under $1 by the README's estimate):

   ```bash
   snow sql -c plant -f sql/03_ai_compare.sql
   ```

   The last result is `ai_trial_scores`: one row per method with `answered` (should be 200)
   and `accuracy_pct`. **Screenshot it.** If AI_CLASSIFY errors on its arguments, note the
   error, delete that statement, and run the file again: the two AI_COMPLETE trials are the
   ones that matter.

2. Set the winner in `sql/04_ai_reasons.sql` (`SET model = '...'`). If two are close, take
   the cheaper (llama3.1-8b). Remove the `--` from `-- LIMIT 20`, then:

   ```bash
   snow sql -c plant -f sql/04_ai_reasons.sql
   snow sql -c plant -q "SELECT d.note, a.ai_reason FROM plant.silver.downtime d JOIN plant.silver.downtime_ai a USING (event_id)"
   ```

   Read the 20 answers. If they look right, put the `--` back and run the file again; it
   skips the 20 already done. The final count should be 2,364 for the model (a few short is
   fine: failed calls write nothing, and running the file again retries them).

3. Rebuild gold and check again:

   ```bash
   snow sql -c plant -f sql/05_gold.sql
   snow sql -c plant -f sql/06_check.sql
   ```

   Still no failed checks. Then in a worksheet:

   ```sql
   SELECT * FROM plant.eval.reason_accuracy ORDER BY method, stop_type;
   SELECT * FROM plant.gold.code_review ORDER BY minutes DESC;
   ```

   **Screenshot both.** Locally, Flash-Next disagreed with the operator's code on 82 stops
   and was right on 79; the Cortex numbers are the ones the README will report.

4. A few hours later (ACCOUNT_USAGE lags), run the cost query at the bottom of
   `03_ai_compare.sql` as ACCOUNTADMIN. **Screenshot it**, along with Admin > Cost
   Management in Snowsight.

## E. Save the results

```bash
mkdir -p results
for t in eval.checks eval.reason_accuracy eval.ai_trial_scores gold.machine_summary \
         gold.downtime_by_reason gold.code_review gold.schedule_adherence gold.delivery_by_week; do
  snow sql -c plant -q "SELECT * FROM plant.$t" --format CSV > "results/$t.csv"
done
```

Screenshots go in `docs/screenshots/`. Worth having beyond the ones marked above: the
database explorer showing the four schemas, and the query profile of the `runs` statement in
`02_silver.sql` (Query History > the query > Query Profile).

Then update the README's numbers and Status, commit, and ask before publishing to GitHub.
Export before day 30: the trial account and its data go away when it ends.

## If something fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `snow connection test` asks for a password or fails with JWT | Public key not set, or a different user | Redo B3; check the fingerprint matches |
| `Private key ... passphrase` error | `PRIVATE_KEY_PASSPHRASE` not set in this terminal | The `read -s` line in B4 |
| PUT: stage does not exist | 00_setup didn't finish | Run step 1 again (everything is IF NOT EXISTS) |
| COPY loads 0 files | Already loaded; Snowflake remembers loaded files for 64 days | Expected on a re-run. To reload, add `FORCE = TRUE` to that COPY |
| A silver statement fails on a timestamp or cast | Something the emulator accepted and Snowflake doesn't | Fix that statement in `02_silver.sql`, re-run 02 (it replaces its tables), then `uv run plantgen emulate` to confirm it still passes locally. Write down what differed: it's a good README note |
| A check fails | Gold differs from truth | `SELECT * FROM plant.eval.checks WHERE NOT pass`; compare with the emulator's output on the same data |
| Cortex: model unavailable in region | Region lacks the model | As ACCOUNTADMIN: `ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'AWS_US';` (check the docs for the current values) |
| Cortex: insufficient privileges | CORTEX_USER grant missing | Re-run the GRANT lines in `00_setup.sql` as ACCOUNTADMIN |
| Cortex: quota or usage limit | Trial limit on AI credits | Wait a day, or add a payment method; 04 picks up where it stopped |
