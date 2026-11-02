import csv
import json
from collections import Counter
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from plantgen import catalog
from plantgen.export import CLEAN, FT_TO_M, TZ, Mess, export
from plantgen.simulate import is_open, simulate

START, DAYS, SEED = date(2026, 8, 3), 18, 7  # ends on a Friday, so some runs are still open


@pytest.fixture(scope="module")
def plant():
    return simulate(START, DAYS, SEED)


def _rows(root: Path, source: str) -> list[dict]:
    out = []
    for f in sorted((root / "raw" / source).iterdir()):
        if f.suffix == ".jsonl":
            out += [json.loads(line) for line in f.read_text().splitlines()]
        else:
            with f.open(newline="") as fh:
                out += list(csv.DictReader(fh))
    return out


def test_same_seed_same_files(plant, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    export(plant, a, Mess(), seed=1)
    export(simulate(START, DAYS, SEED), b, Mess(), seed=1)
    files_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files_a == sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert all((a / f).read_bytes() == (b / f).read_bytes() for f in files_a)


def test_runs_are_consistent(plant):
    wos = {w.wo_id for w in plant.work_orders}
    for r in plant.runs:
        assert r.wo_id in wos
        assert r.end > r.start
        assert is_open(r.start), f"{r.run_id} started while the plant was closed"
        assert 0 <= r.scrap_ft <= r.produced_ft
    for eq in catalog.EQUIPMENT:
        runs = sorted((r for r in plant.runs if r.equipment == eq.code), key=lambda r: r.start)
        assert all(a.end <= b.start for a, b in pairwise(runs)), f"{eq.code} double-booked"


def test_slitting_follows_coating(plant):
    coat = {r.wo_id: r for r in plant.runs if r.step == "coat"}
    for r in (r for r in plant.runs if r.step == "slit"):
        assert r.start >= coat[r.wo_id].end
        assert r.produced_ft == coat[r.wo_id].good_ft


def test_stops_inside_their_run(plant):
    runs = {r.run_id: r for r in plant.runs}
    for d in plant.downtime:
        if d.run_id and d.reason != "CLEANING":
            run = runs[d.run_id]
            assert run.start <= d.start and d.end <= run.end, d.event_id


def test_clean_export_matches_truth(plant, tmp_path):
    export(plant, tmp_path, CLEAN, seed=1)
    done = {r.run_id: r for r in plant.runs if r.end <= plant.horizon}

    coater = [r for r in _rows(tmp_path, "coater_runs") if r["end_time"]]
    assert {r["run_id"] for r in coater} == {k for k, r in done.items() if r.step == "coat"}
    for row in coater:
        truth = done[row["run_id"]]
        assert int(row["good_ft"]) == truth.good_ft
        assert datetime.fromisoformat(row["start_time"]) == truth.start.replace(tzinfo=TZ)

    for row in _rows(tmp_path, "slitter_runs"):
        assert float(row["Meters_Good"]) == pytest.approx(
            done[row["JobNo"]].good_ft * FT_TO_M, abs=0.06
        )

    codes = {r.hmi_code[:2]: r.code for r in catalog.REASONS}
    truth = {d.event_id: d.reason for d in plant.downtime}
    for e in _rows(tmp_path, "hmi_downtime"):
        assert codes[e["reason"]["code"]] == truth[e["event_id"]]
        start = datetime.fromtimestamp(e["ts_start"] / 1000, TZ).replace(tzinfo=None)
        assert start == next(d.start for d in plant.downtime if d.event_id == e["event_id"])


def test_messy_export_has_the_promised_quirks(plant, tmp_path):
    export(plant, tmp_path, Mess(), seed=1)
    coater = _rows(tmp_path, "coater_runs")
    open_ids = {r["run_id"] for r in coater if not r["end_time"]}
    assert open_ids, "expected runs that cross midnight or are still running at the end"
    assert any(r.end > plant.horizon for r in plant.runs), "expected runs still open at the end"

    events = _rows(tmp_path, "hmi_downtime")
    reasons = Counter(None if e["reason"] is None else e["reason"]["code"] for e in events)
    assert reasons["99"] > 0 and reasons[None] > 0
    assert len({e["event_id"] for e in events}) < len(events), "expected duplicate rows"

    aliases = {r["line"] for r in coater} | {e["asset"] for e in events}
    assert aliases - {e.code for e in catalog.EQUIPMENT}, "expected odd equipment spellings"

    wo_rows = _rows(tmp_path, "erp_work_orders")
    assert max(Counter(r["WO_NUM"] for r in wo_rows).values()) > 2, "expected order history"


def test_truth_summary(plant, tmp_path):
    export(plant, tmp_path, Mess(), seed=1)
    summary = json.loads((tmp_path / "truth" / "summary.json").read_text())
    # two full weeks, then Monday 06:00 to Friday 00:00
    assert summary["open_hours_per_machine"] == 2 * 120 + 90
    for eq in summary["equipment"].values():
        assert eq["good_ft"] + eq["scrap_ft"] == eq["produced_ft"]
