"""plantgen generate: write a synthetic plant's exports to disk."""

import argparse
import json
import shutil
from datetime import date
from pathlib import Path

from plantgen.export import CLEAN, Mess, export
from plantgen.simulate import simulate


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="plantgen")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="simulate the plant and write raw/, reference/, truth/")
    g.add_argument("--out", type=Path, default=Path("data"))
    g.add_argument(
        "--start",
        type=date.fromisoformat,
        default=date(2026, 8, 3),
        help="first day (default 2026-08-03, a Monday)",
    )
    g.add_argument(
        "--days",
        type=int,
        default=88,
        help="default 88: ends on a Friday, so some runs are still open",
    )
    g.add_argument("--seed", type=int, default=7)
    g.add_argument(
        "--clean",
        action="store_true",
        help="no duplicates, late rows, odd spellings or missing HMI codes",
    )
    e = sub.add_parser("emulate", help="run sql/ on a local Snowflake emulator and check gold")
    e.add_argument("--data", type=Path, default=Path("data"))
    e.add_argument("--sql", type=Path, default=Path("sql"))
    e.add_argument(
        "--db", type=Path, help="keep the emulator's databases in this folder between runs"
    )
    e.add_argument(
        "--classify",
        metavar="URL",
        help="classify downtime notes with a local OpenAI-compatible server, e.g. "
        "http://127.0.0.1:11439 (stands in for Cortex)",
    )
    e.add_argument("--model", default="flash-next-local", help="label stored with each answer")
    e.add_argument("--sample", type=int, help="classify only this many stops")
    e.add_argument("--batch", type=int, default=20, help="stops per request (default 20)")
    args = p.parse_args(argv)
    if args.cmd == "emulate":
        _emulate(args)
    else:
        _generate(args)


def _generate(args: argparse.Namespace) -> None:
    for sub_dir in ("raw", "reference", "truth"):
        shutil.rmtree(args.out / sub_dir, ignore_errors=True)
    plant = simulate(args.start, args.days, args.seed)
    counts = export(plant, args.out, CLEAN if args.clean else Mess(), seed=args.seed + 1)

    summary = json.loads((args.out / "truth" / "summary.json").read_text())
    print(f"{plant.start} to {plant.end} (exclusive), seed {args.seed} -> {args.out}/")
    for source, n in sorted(counts.items()):
        print(f"  raw/{source:<16} {n:>6} rows")
    print(
        f"  work orders {summary['work_orders']}, completed {summary['completed_orders']}, "
        f"cancelled {summary['cancelled']}"
    )
    for code, eq in summary["equipment"].items():
        util = eq["running_hours"] / summary["open_hours_per_machine"]
        down = sum(eq["downtime_min_by_reason"].values()) / 60
        print(
            f"  {code}: {eq['runs']} runs, running {util:.0%} of open hours, {down:.0f} h downtime"
        )


def _emulate(args: argparse.Namespace) -> None:
    from plantgen import classify, emulate

    step = (
        classify.classifier(args.classify, args.model, args.sample, args.batch)
        if args.classify
        else None
    )
    out = emulate.run(args.data, args.sql, args.db, step)
    checks = out["eval.checks"]
    failed = [c for c in checks if not c["pass"]]
    print(f"checks: {len(checks) - len(failed)} passed, {len(failed)} failed")
    for c in failed:
        print(
            f"  FAIL {c['area']}/{c['item']} {c['metric']}: "
            f"expected {c['expected']}, got {c['actual']}"
        )
    print("machine summary:")
    for m in out["gold.machine_summary"]:
        print(
            f"  {m['machine']}: {m['runs']} runs, {m['running_hours']} h running, "
            f"availability {m['availability_pct']}%, utilization {m['utilization_pct']}%, "
            f"scrap {m['scrap_pct']}%"
        )
    review = out["gold.code_review"]
    print(
        f"codes worth a second look: {len(review)} stops, "
        f"{sum(r['minutes'] for r in review):.0f} min (gold.code_review)"
    )
    print("reason accuracy:")
    for r in sorted(out["eval.reason_accuracy"], key=lambda r: (r["method"], r["stop_type"])):
        print(
            f"  {r['method']:<34} {r['stop_type']:<10} {r['correct']:>5}/{r['stops']:<5} "
            f"{r['accuracy_pct']}% of stops, {r['minutes_right_pct']}% of minutes"
        )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
