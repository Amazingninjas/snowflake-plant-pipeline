"""Run the pipeline's SQL on fakesnow, a local Snowflake emulator built on DuckDB.

The emulator can't read files from a stage, so load_bronze() stands in for the PUT and COPY in
sql/01_bronze.sql: it runs that file's CREATE TABLE statements, then inserts each file's rows
itself, with the same _file and _row columns COPY would add. Silver, gold and the checks run
from the repo's SQL files as written.

It can't call Cortex either. replay() loads the answers Cortex gave on the Snowflake run
(results/snowflake/, written by scripts/export.sh) in place of 03 and 04, and compare() checks
every table against Snowflake's copy, so the whole run can be shown without an account.
DuckDB accepts some SQL that Snowflake refuses; SNOWFLAKE_RULES catches the ones the Snowflake
run tripped over.

Needs the "emulator" dependency group (uv sync installs it with dev).
"""

import csv
import logging
import re
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

RAW_SOURCES = (
    "erp_work_orders",
    "coater_runs",
    "slitter_runs",
    "hmi_downtime",
    "qc_defects",
    "schedule",
)
FILE_TABLES = {  # table -> file under data/
    "bronze.ref_products": "reference/products.csv",
    "bronze.ref_equipment": "reference/equipment.csv",
    "bronze.ref_operators": "reference/operators.csv",
    "bronze.ref_downtime_codes": "reference/downtime_codes.csv",
    "eval.downtime_truth": "truth/downtime_truth.csv",
    "eval.runs_truth": "truth/runs_truth.csv",
}
CHUNK = 250  # rows per INSERT
REPLAYED = ("silver.downtime_ai", "eval.ai_trials")  # the Cortex answers, as Snowflake gave them

# SQL that DuckDB runs and Snowflake refuses, with Snowflake's error. run_file() checks each
# statement, so these fail here instead of on the account.
SNOWFLAKE_RULES = (
    (
        re.compile(
            r"LATERAL\s+FLATTEN\s*\((?:[^()]|\([^()]*\))*\)\s*(?:AS\s+)?\w*\s+"
            r"(?:(?:LEFT|RIGHT|FULL|INNER)\s+(?:OUTER\s+)?)?JOIN\b",
            re.I,
        ),
        "Lateral View cannot be on the left side of join (flatten in a CTE, then join to it)",
    ),
)


class SnowflakeRuleError(ValueError):
    """A statement the emulator would run but Snowflake refuses."""


def check_rules(stmt: str) -> None:
    for pattern, message in SNOWFLAKE_RULES:
        if pattern.search(stmt):
            raise SnowflakeRuleError(f"{message}:\n{stmt[:300]}")


def split_sql(text: str) -> list[str]:
    """Statements in a SQL file, without comments. Knows about quotes; nothing fancier."""
    out, buf, quoted, i = [], [], False, 0
    while i < len(text):
        c = text[i]
        if c == "'":
            quoted = not quoted
        elif not quoted and text.startswith("--", i):
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        elif not quoted and c == ";":
            out.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf).strip())
    return [s for s in out if s]


@contextmanager
def connect(db_path: Path | None = None) -> Iterator:
    """A cursor on the emulator; with db_path, databases persist there between runs."""
    import fakesnow
    import fakesnow.transforms
    import snowflake.connector

    logging.getLogger("sqlglot").setLevel(logging.ERROR)  # "Unsupported property" chatter
    warnings.filterwarnings("ignore", message="Arrow output", category=UserWarning)
    # fakesnow wraps the argument of SUM, AVG and the like in TRY_CAST(... AS DOUBLE), so they
    # take text the way Snowflake does. That turns exact decimal sums into floats, and a total
    # of exactly .x5 then rounds the other way. Silver casts everything, so nothing here sums
    # text: switch the rewrite off.
    exact_sums = mock.patch.object(fakesnow.transforms, "numeric_agg_implicit_cast", lambda e: e)
    with fakesnow.patch(db_path=str(db_path) if db_path else None), exact_sums:
        conn = snowflake.connector.connect()
        try:
            yield conn.cursor()
        finally:
            conn.close()


def run_file(cur, path: Path, keep: Callable[[str], bool] = lambda s: True) -> None:
    for stmt in split_sql(path.read_text()):
        if keep(stmt):
            check_rules(stmt)
            cur.execute(stmt)


def _columns(cur, table: str) -> list[str]:
    cur.execute(f"SELECT * FROM {table} LIMIT 0")
    return [d[0] for d in cur.description]


def _insert(cur, table: str, columns: list[str], rows: list[list], json_first: bool = False):
    marks = "(" + ", ".join(["%s"] * len(columns)) + ")"
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        values = ", ".join([marks] * len(chunk))
        params = [None if v == "" else v for row in chunk for v in row]
        if json_first:  # VARIANT: Snowflake only takes PARSE_JSON in a SELECT, not in VALUES
            rest = ", ".join(f"column{n}" for n in range(2, len(columns) + 1))
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"SELECT PARSE_JSON(column1), {rest} FROM VALUES {values}",
                params,
            )
        else:
            cur.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {values}", params)


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def load_bronze(cur, sql_dir: Path, data: Path) -> None:
    """What PUT + sql/01_bronze.sql would do, minus the stage."""
    skip = ("COPY ", "CREATE FILE FORMAT", "CREATE STAGE", "SELECT")
    run_file(cur, sql_dir / "01_bronze.sql", keep=lambda s: not s.upper().startswith(skip))
    cur.execute("USE DATABASE plant")
    for source in RAW_SOURCES:
        table = f"bronze.{source}"
        cur.execute(f"DELETE FROM {table}")  # COPY loads each file once; so do we
        cols = _columns(cur, table)
        for f in sorted((data / "raw" / source).iterdir()):
            name = f"{source}/{f.name}.gz"  # METADATA$FILENAME after PUT's AUTO_COMPRESS
            if f.suffix == ".jsonl":
                lines = f.read_text().splitlines()
                rows = [[line, name, n] for n, line in enumerate(lines, 1)]
                _insert(cur, table, ["v", "_file", "_row"], rows, json_first=True)
            else:
                header, rows = _read_csv(f)
                n = len(header)
                rows = [r + [name, k] for k, r in enumerate(rows, 1)]
                _insert(cur, table, [*cols[:n], "_file", "_row"], rows)
    for table, rel in FILE_TABLES.items():
        _, rows = _read_csv(data / rel)
        _insert(cur, table, _columns(cur, table), rows)
    summary = (data / "truth" / "summary.json").read_text()
    _insert(cur, "eval.summary", ["v"], [[summary]], json_first=True)
    prompts = sorted((sql_dir.parent / "prompts").glob("*.txt"))
    _insert(cur, "bronze.prompts", ["name", "text"], [[f.stem, f.read_text()] for f in prompts])


def replay(results: Path, sql_dir: Path) -> Callable:
    """Stand-in for 03 and 04: load the answers Cortex gave on Snowflake instead of asking a
    model, then score the model comparison with 03's own SQL."""
    compare_sql = sql_dir / "03_ai_compare.sql"

    def use(s: str) -> bool:
        return s.upper().startswith("USE ")

    def step(cur) -> None:
        run_file(cur, compare_sql, keep=lambda s: use(s) or s.upper().startswith("CREATE TABLE"))
        for table in REPLAYED:
            header, rows = _read_csv(results / f"{table}.csv")
            cur.execute(f"DELETE FROM {table}")
            _insert(cur, table, [h.lower() for h in header], rows)
        run_file(cur, compare_sql, keep=lambda s: use(s) or "TABLE AI_TRIAL_SCORES" in s.upper())

    return step


def _kind(rows: list[dict], name: str) -> type | None:
    value = next((r[name] for r in rows if r.get(name) is not None), None)
    if isinstance(value, bool):
        return bool
    if isinstance(value, int | float | Decimal):
        return float
    return type(value) if value is not None else None


def _norm(value, kind: type | None = None) -> str:
    """One value as both sides can agree on it: NULL as empty (that's how the CSV has it),
    numbers to 4 places (Snowflake keeps 6-place decimals where DuckDB has floats),
    timestamps in UTC. With kind, value is CSV text from Snowflake."""
    if value is None or value == "":
        return ""
    if kind is not None:  # text from the CSV, read as the emulator's type for that column
        if kind is float:
            value = float(value)
        elif kind is datetime:
            value = datetime.fromisoformat(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float | Decimal):
        return f"{round(float(value), 4) + 0.0:.4f}"
    if isinstance(value, datetime):
        if value.tzinfo:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def compare(cur, results: Path) -> list[dict]:
    """Each table exported from Snowflake against the emulator's copy, row for row in any
    order. Returns, per table, the rows only one side has."""
    out = []
    for path in sorted(results.glob("*.csv")):
        header, rows = _read_csv(path)
        names = [h.lower() for h in header]
        mine = query(cur, f"SELECT * FROM plant.{path.stem}")
        kinds = [_kind(mine, n) or str for n in names]
        theirs = Counter(tuple(map(_norm, r, kinds)) for r in rows)
        ours = Counter(tuple(_norm(r.get(n)) for n in names) for r in mine)
        out.append(
            {
                "table": path.stem,
                "rows": len(rows),
                "only_snowflake": list((theirs - ours).elements()),
                "only_emulator": list((ours - theirs).elements()),
            }
        )
    return out


def run(
    data: Path,
    sql_dir: Path,
    db_path: Path | None = None,
    classify: Callable | None = None,
    compare_to: Path | None = None,
) -> dict[str, list[dict]]:
    """Setup, bronze, silver, (classify), gold, checks. Returns the eval tables as dicts; with
    compare_to, also "snowflake": compare()'s result."""
    with connect(db_path) as cur:
        run_file(
            cur,
            sql_dir / "00_setup.sql",
            keep=lambda s: s.upper().startswith(("CREATE DATABASE", "CREATE SCHEMA")),
        )
        load_bronze(cur, sql_dir, data)
        run_file(cur, sql_dir / "02_silver.sql")
        if classify:
            classify(cur)
        run_file(cur, sql_dir / "05_gold.sql")
        run_file(cur, sql_dir / "06_check.sql")
        out = {t: query(cur, f"SELECT * FROM plant.{t}") for t in TABLES}
        if compare_to:
            out["eval.ai_trial_scores"] = query(cur, "SELECT * FROM plant.eval.ai_trial_scores")
            out["snowflake"] = compare(cur, compare_to)
        return out


TABLES = (
    "eval.checks",
    "eval.reason_accuracy",
    "gold.code_review",
    "gold.machine_summary",
    "gold.schedule_adherence",
    "gold.scrap_pareto",
)


def query(cur, sql: str, params=None) -> list[dict]:
    cur.execute(sql, params)
    names = [d[0].lower() for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
