"""The SQL pipeline on the local emulator, checked against the generator's truth."""

import re
from datetime import date
from pathlib import Path

import pytest

from plantgen import catalog
from plantgen.classify import batch_prompt
from plantgen.emulate import SnowflakeRuleError, check_rules, replay, run, split_sql
from plantgen.export import CLEAN, Mess, export
from plantgen.simulate import simulate

REPO = Path(__file__).resolve().parents[1]
SQL = REPO / "sql"
SNOWFLAKE_RUN = REPO / "results" / "snowflake"
PROMPT = (REPO / "prompts" / "downtime_reason.txt").read_text()
START, DAYS, SEED = date(2026, 8, 3), 18, 7


@pytest.fixture(scope="module")
def plant():
    return simulate(START, DAYS, SEED)


@pytest.mark.parametrize("mess", [Mess(), CLEAN], ids=["messy", "clean"])
def test_gold_matches_truth(plant, tmp_path, mess):
    export(plant, tmp_path, mess, seed=1)
    out = run(tmp_path, SQL)
    failed = [c for c in out["eval.checks"] if not c["pass"]]
    assert not failed
    assert len(out["eval.checks"]) > 50
    if mess is CLEAN:
        coded = [r for r in out["eval.reason_accuracy"] if r["method"] == "hmi code, all stops"]
        assert all(r["accuracy_pct"] == 100 for r in coded)


def test_code_review_lists_the_wrong_codes(plant, tmp_path):
    # A stand-in AI that always gives the true reason: every stop where it disagrees with the
    # operator's code is a wrong code, and gold.code_review should hold exactly those.
    def perfect(cur):
        cur.execute(
            "INSERT INTO plant.silver.downtime_ai (event_id, ai_reason, model) "
            "SELECT event_id, reason, 'truth' FROM plant.eval.downtime_truth"
        )

    export(plant, tmp_path, Mess(), seed=1)
    out = run(tmp_path, SQL, classify=perfect)
    assert not [c for c in out["eval.checks"] if not c["pass"]]
    acc = {(r["method"], r["stop_type"]): r for r in out["eval.reason_accuracy"]}
    wrong = sum(
        r["stops"] - r["correct"] for (m, _), r in acc.items() if m == "hmi code, coded stops only"
    )
    assert wrong > 0
    assert len(out["gold.code_review"]) == wrong
    assert all(r["correct"] == 0 for (m, _), r in acc.items() if m == "code review stops, hmi code")


def test_emulator_reproduces_the_snowflake_run(tmp_path):
    # The default data (what plantgen generate writes, and what went to Snowflake), with the
    # answers Cortex gave there: every table exported from the trial run should come out the
    # same, row for row.
    export(simulate(date(2026, 8, 3), 88, 7), tmp_path, Mess(), seed=8)
    out = run(tmp_path, SQL, classify=replay(SNOWFLAKE_RUN, SQL), compare_to=SNOWFLAKE_RUN)
    assert not [c for c in out["eval.checks"] if not c["pass"]]
    assert len(out["snowflake"]) == 25
    differ = {t["table"]: t for t in out["snowflake"] if t["only_snowflake"] or t["only_emulator"]}
    assert not differ


def test_flatten_on_the_left_of_a_join_is_refused():
    # 06_check.sql had this shape once: DuckDB ran it, Snowflake refused it.
    bad = (
        "SELECT e.key, p.records FROM summary s, LATERAL FLATTEN(input => s.v:scrap) e "
        "LEFT JOIN gold.scrap_pareto p ON p.defect_type = e.key"
    )
    with pytest.raises(SnowflakeRuleError):
        check_rules(bad)
    check_rules("SELECT e.key FROM summary s, LATERAL FLATTEN(input => s.v:scrap) e")


def test_split_sql_ignores_comments_and_quoted_semicolons():
    text = "SELECT 'a;b' AS x; -- a comment; with a semicolon\nSELECT 'it''s' -- tail\n;"
    assert split_sql(text) == ["SELECT 'a;b' AS x", "SELECT 'it''s'"]


def test_prompt_and_sql_agree_on_reasons():
    codes = {r.code for r in catalog.REASONS}
    listed = dict(re.findall(r"^([A-Z_]+): (.+)$", PROMPT, flags=re.M))
    assert set(listed) == codes
    enums = re.findall(r"'enum': \[(.*?)\]", (SQL / "03_ai_compare.sql").read_text(), flags=re.S)
    assert enums and all(set(re.findall(r"'([A-Z_]+)'", e)) == codes for e in enums)
    # AI_CLASSIFY gets the same descriptions, without the final period
    for name in ("03_ai_compare.sql", "04_ai_reasons.sql"):
        text_sql = (SQL / name).read_text().replace("''", "'")
        for code, text in listed.items():
            assert f"{{'label': '{code}', 'description': '{text.rstrip('.')}'}}" in text_sql


def test_batch_prompt_keeps_the_instructions_once():
    one = batch_prompt(PROMPT, [("coater", "glue pump lost prime")])
    assert one.endswith("Machine: coater\nNote: glue pump lost prime\n")
    many = batch_prompt(PROMPT, [("coater", "a"), ("slitter", "b")])
    assert many.count("Reasons:") == 1
    assert many.endswith("1. Machine: coater | Note: a\n2. Machine: slitter | Note: b\n")
