"""Run the pipeline's SQL on fakesnow, a local Snowflake emulator built on DuckDB.

The emulator can't read files from a stage, so load_bronze() stands in for the PUT and COPY in
sql/01_bronze.sql: it runs that file's CREATE TABLE statements, then inserts each file's rows
itself, with the same _file and _row columns COPY would add. Silver, gold and the checks run
from the repo's SQL files as written. Passing here means the logic is right; Snowflake can still
disagree on details, so the run on Snowflake is the test that counts.

Needs the "emulator" dependency group (uv sync installs it with dev).
"""

import csv
import logging
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

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
    import snowflake.connector

    logging.getLogger("sqlglot").setLevel(logging.ERROR)  # "Unsupported property" chatter
    warnings.filterwarnings("ignore", message="Arrow output", category=UserWarning)
    with fakesnow.patch(db_path=str(db_path) if db_path else None):
        conn = snowflake.connector.connect()
        try:
            yield conn.cursor()
        finally:
            conn.close()


def run_file(cur, path: Path, keep: Callable[[str], bool] = lambda s: True) -> None:
    for stmt in split_sql(path.read_text()):
        if keep(stmt):
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


def run(
    data: Path,
    sql_dir: Path,
    db_path: Path | None = None,
    classify: Callable | None = None,
) -> dict[str, list[dict]]:
    """Setup, bronze, silver, (classify), gold, checks. Returns the eval tables as dicts."""
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
        return {t: query(cur, f"SELECT * FROM plant.{t}") for t in TABLES}


TABLES = (
    "eval.checks",
    "eval.reason_accuracy",
    "gold.machine_summary",
    "gold.schedule_adherence",
    "gold.scrap_pareto",
)


def query(cur, sql: str, params=None) -> list[dict]:
    cur.execute(sql, params)
    names = [d[0].lower() for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
