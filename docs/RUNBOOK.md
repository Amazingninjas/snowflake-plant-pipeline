# Trial runbook

Every step of the Snowflake run, in order, with what each one should show. The 30-day clock
starts at signup, so do part A first; it needs no account. Run commands from the repo root.
Expected counts are from the local emulator on the default data (`plantgen generate`, 88
days, seed 7); the run on 2026-11-07/08 matched every one of them.

`scripts/snowrun.sh sql/<file>.sql` does the same as `snow sql -c plant -f sql/<file>.sql`
and keeps the output in `logs/<file>.log` (not committed).

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
   without cross-region calls. (This run's signup didn't offer a choice and landed in
   AWS_CA_CENTRAL_1; D1 turns on cross-region calls for that case.)
2. Activate from the email, choose a username and password, and enroll MFA when Snowsight
   asks.
3. In a Snowsight worksheet (Projects > Workspaces, or Worksheets):

   ```sql
   SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS account, CURRENT_USER();
   ```

   Then give your user the public key. Trial usernames can be stored in lowercase, and
   Snowflake uppercases unquoted names, so quote the exact name with `IDENTIFIER`. This
   prints the statements to paste (the public key isn't secret):

   ```bash
   echo "USE ROLE ACCOUNTADMIN;
   SET me = '\"' || CURRENT_USER() || '\"';
   ALTER USER IDENTIFIER(\$me) SET RSA_PUBLIC_KEY = '$(grep -v 'PUBLIC KEY' ~/.snowflake/keys/rsa_key.pub | tr -d '\n')';
   DESC USER IDENTIFIER(\$me);"
   ```

   Highlight all four lines in the worksheet before pressing Ctrl+Enter: without a
   selection, Snowsight runs only the statement under the cursor. Generate the key pair
   once: running A2 again makes a new pair, and the registered public key stops matching.
   The `RSA_PUBLIC_KEY_FP` row of DESC USER should match:

   ```bash
   echo "SHA256:$(openssl rsa -pubin -in ~/.snowflake/keys/rsa_key.pub -outform DER | openssl dgst -sha256 -binary | openssl enc -base64)"
   ```

   If a statement fails with "User ... does not exist or not authorized", run
   `SELECT CURRENT_USER(), CURRENT_ROLE();` on its own: a lowercase name means the quoting
   above was skipped, and a role other than ACCOUNTADMIN means the `USE ROLE` line failed.

4. Find your login name: `SHOW USERS;` in the worksheet, column `login_name`. Key-pair
   logins match the login name, not the user name, and a trial's login name can be your
   email (on this run it was, in capitals, while the user name was not). Then add the
   connection and test it. `read -s` keeps the passphrase off the screen and out of
   shell history; it lasts until the terminal closes.

   ```bash
   snow connection add --connection-name plant --account <ORG-ACCOUNT from B3> \
     --user <login_name> --authenticator SNOWFLAKE_JWT \
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
which it doesn't cap, so the guard here is one cheap call first, then 200 stops, then all.

1. **Turn AI on and try one call.** Self-service trials have every AI function switched off
   until a credit card is on the account ("AI function ... is not available for trial
   accounts"). As ACCOUNTADMIN, add one under Admin > Billing. That doesn't upgrade or end
   the trial, and the calls still draw on the free credits; don't click Upgrade, which does
   both. Then, in a worksheet:

   ```sql
   USE ROLE plant_engineer;
   USE WAREHOUSE plant_wh;
   SELECT AI_COMPLETE('llama3.1-8b', 'Reply with one word: OK') AS small_model,
          AI_COMPLETE('llama3.3-70b', 'Reply with one word: OK') AS big_model;
   ```

   Both should say OK. If a model is "unavailable in your region", run
   `USE ROLE ACCOUNTADMIN; ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'AWS_US';` and try
   again. (This run did both, on an account in AWS_CA_CENTRAL_1.)

2. **Compare three methods on 200 stops** (600 calls, a few minutes; 0.58 AI credits on this
   run):

   ```bash
   snow sql -c plant -f sql/03_ai_compare.sql
   ```

   The last result is `ai_trial_scores`, one row per method; `answered` should be 200. This
   run: AI_CLASSIFY 200 right, llama3.3-70b 195, llama3.1-8b 177. `04_ai_reasons.sql` uses
   AI_CLASSIFY; if your scores pick another method, swap in that method's call from 03.

3. **Classify every stop.** The 200-stop comparison already served as the small trial run,
   so this goes straight to all of them (`04_ai_reasons.sql` has a `LIMIT` line to remove if
   you want a smaller first try):

   ```bash
   snow sql -c plant -f sql/04_ai_reasons.sql
   snow sql -c plant -f sql/05_gold.sql
   snow sql -c plant -f sql/06_check.sql
   ```

   04 ends with one row: `ai_classify | 2364` (this run took under a minute and 6.36 AI
   credits, about $12.70; AI_CLASSIFY bills about 1,660 tokens a stop). Answers are
   kept, so running it again only pays for stops that have no answer yet. 06 should still
   show no failed checks. Then in a worksheet, one query at a time:

   ```sql
   SELECT * FROM plant.eval.reason_accuracy ORDER BY method, stop_type;
   SELECT * FROM plant.gold.code_review ORDER BY minutes DESC;
   ```

   **Screenshot both.**

4. **Check what it cost**, a few hours later (ACCOUNT_USAGE lags; this run's usage was all
   there after four hours). As ACCOUNTADMIN, run the two cost queries at the bottom of
   `03_ai_compare.sql`, with your trial's start date, and download each result into
   `results/cost/`. Admin > Cost Management in Snowsight shows the same usage. This run: 6.942
   AI credits, 6.897 of them AI_CLASSIFY, plus 0.393 for the warehouse.

## E. Save the results and check the emulator against them

```bash
scripts/export.sh
uv run plantgen emulate --replay results/snowflake
```

`export.sh` writes every silver, gold and eval table to `results/snowflake/<schema>.<table>.csv`
(25 tables, rows sorted so a re-export of unchanged tables gives the same files). The second
command runs the whole pipeline on the local emulator with Cortex's answers from that export
in place of the AI calls, then compares every table with Snowflake's. It should end:
`same as Snowflake: 25 of 25 tables row for row`. The tests run the same comparison, so CI
re-checks it on every push.

Screenshots go in `docs/screenshots/`. Then update the README's numbers and Status, commit,
and ask before publishing to GitHub. Export before day 30: the trial account and its data go
away when it ends.

## If something fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `snow connection test` fails with `JWT token is invalid` | The registered public key doesn't match `rsa_key.pub` (the key was regenerated, or the ALTER didn't run), or `--user` isn't the login name | `SHOW USERS;` shows `has_rsa_public_key` and `login_name`; re-run the ALTER from B3 with the current `rsa_key.pub` |
| `Private key ... passphrase` error | `PRIVATE_KEY_PASSPHRASE` not set in this terminal | The `read -s` line in B4 |
| PUT: stage does not exist | 00_setup didn't finish | Run step 1 again (everything is IF NOT EXISTS) |
| COPY loads 0 files | Already loaded; Snowflake remembers loaded files for 64 days | Expected on a re-run. To reload, add `FORCE = TRUE` to that COPY |
| A silver statement fails on a timestamp or cast | Something the emulator accepted and Snowflake doesn't | Fix that statement in `02_silver.sql`, re-run 02 (it replaces its tables), then `uv run plantgen emulate` to confirm it still passes locally. Write down what differed: it's a good README note |
| `Lateral View cannot be on the left side of join` | A `LATERAL FLATTEN` followed by a JOIN in the same FROM; DuckDB allows it | Flatten in its own CTE, then join to the CTE (how `06_check.sql` does it). The emulator now refuses this shape too |
| A check fails | Gold differs from truth | `SELECT * FROM plant.eval.checks WHERE NOT pass`; compare with the emulator's output on the same data |
| Cortex: model unavailable in region | Region lacks the model | As ACCOUNTADMIN: `ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'AWS_US';` (check the docs for the current values) |
| Cortex: insufficient privileges | CORTEX_USER grant missing | Re-run the GRANT lines in `00_setup.sql` as ACCOUNTADMIN |
| Cortex: `AI function ... is not available for trial accounts` | AI features are off on self-service trials until a credit card is on the account | As ACCOUNTADMIN, add a card under Admin > Billing. Adding a card doesn't upgrade the trial or end it, and the AI calls still draw on the free credits. Don't click Upgrade: that ends the trial and starts on-demand billing |
| Cortex: quota or usage limit | Trial limit on AI credits | Wait a day; 04 picks up where it stopped |
