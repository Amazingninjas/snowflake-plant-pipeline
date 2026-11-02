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
    args = p.parse_args(argv)

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


if __name__ == "__main__":
    main()
