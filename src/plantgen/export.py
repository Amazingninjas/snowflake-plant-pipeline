"""Write the plant's records the way each source system would export them.

Layout under the output folder:
  raw/<source>/<one file per day>   what bronze loads; each source has its own quirks
  reference/                        small lookup tables (products, equipment, operators, codes)
  truth/                            the answers, for checking silver/gold and the AI step;
                                    never load these into bronze

Quirks, on purpose (silver has to deal with all of them):
  erp_work_orders  one row per change; latest LAST_MOD wins; MM/DD/YYYY due dates
  coater_runs      ISO-8601 with UTC offset; a run that crosses midnight shows up open (blank
                   end) in its start-day file and complete in its end-day file
  slitter_runs     another vendor: meters, MM/DD/YYYY HH:MM:SS local time, different headers
  hmi_downtime     JSON lines, epoch milliseconds, nested operator, reason code usually OTHER
                   or missing (the free-text note is the real information)
  qc_defects       defect types spelled several ways
  schedule         new plan assignments per day
  all              equipment spelled several ways, badge ids with stray spaces/case,
                   ~1% duplicate rows, ~3% of rows land in the next day's file
"""

import csv
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from plantgen import catalog
from plantgen.simulate import Downtime, Plant, Run, open_hours

TZ = ZoneInfo("America/New_York")
FT_TO_M = 0.3048


@dataclass(frozen=True)
class Mess:
    duplicate_rate: float = 0.01
    late_rate: float = 0.03
    alias_rate: float = 0.5  # rows that spell equipment some other way
    badge_rate: float = 0.2  # rows with a sloppy badge id
    hmi_right: float = 0.40  # unplanned stops: operator picked the right HMI code
    hmi_other: float = 0.45  # picked "99 OTHER"; then blank 10%, wrong code the rest
    hmi_blank: float = 0.10
    hmi_planned_right: float = 0.9  # changeovers, cleaning, PM: usually coded right


CLEAN = Mess(
    duplicate_rate=0,
    late_rate=0,
    alias_rate=0,
    badge_rate=0,
    hmi_right=1.0,
    hmi_other=0,
    hmi_blank=0,
    hmi_planned_right=1.0,
)

OTHER_CODE = "99 OTHER"


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=TZ)


def _iso_offset(t: datetime) -> str:
    return _aware(t).isoformat(timespec="seconds")


def _epoch_ms(t: datetime) -> int:
    return int(_aware(t).timestamp() * 1000)


class _Writer:
    def __init__(self, root: Path, plant: Plant, mess: Mess, seed: int) -> None:
        self.root = root
        self.plant = plant
        self.mess = mess
        self.rng = random.Random(seed)
        self.counts: dict[str, int] = defaultdict(int)

    # messing helpers

    def equipment(self, code: str) -> str:
        eq = next(e for e in catalog.EQUIPMENT if e.code == code)
        return self.rng.choice(eq.aliases) if self.rng.random() < self.mess.alias_rate else code

    def badge(self, badge: str) -> str:
        if self.rng.random() >= self.mess.badge_rate:
            return badge
        return self.rng.choice(
            (f" {badge}", f"{badge} ", badge.lower(), f"{badge[:2]} {badge[2:]}")
        )

    def spread(self, rows_by_day: dict[date, list[list]]) -> dict[date, list[list]]:
        """Duplicate a few rows and push a few into the next day's file."""
        last = self.plant.end - timedelta(days=1)
        out: dict[date, list[list]] = defaultdict(list)
        for day in sorted(rows_by_day):
            for row in rows_by_day[day]:
                target = day
                if day < last and self.rng.random() < self.mess.late_rate:
                    target = day + timedelta(days=1)
                out[target].append(row)
                if self.rng.random() < self.mess.duplicate_rate:
                    out[target].append(list(row))
        return out

    def write_csv(self, source: str, name: str, header: list[str], rows: list[list]) -> None:
        path = self.root / "raw" / source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        self.counts[source] += len(rows)

    # sources

    def erp_work_orders(self) -> None:
        rows: dict[date, list[list]] = defaultdict(list)
        for wo in self.plant.work_orders:
            for v in wo.versions:
                if v.modified < self.plant.horizon:
                    rows[v.modified.date()].append(
                        [
                            wo.wo_id,
                            wo.customer_no,
                            wo.customer_name,
                            wo.product.sku,
                            v.qty_ft,
                            v.due.strftime("%m/%d/%Y"),
                            v.status,
                            v.modified.strftime("%Y-%m-%d %H:%M:%S"),
                        ]
                    )
        header = [
            "WO_NUM",
            "CUST_NO",
            "CUST_NAME",
            "ITEM",
            "QTY_LF",
            "DUE_DT",
            "STATUS",
            "LAST_MOD",
        ]
        for day, day_rows in sorted(self.spread(rows).items()):
            self.write_csv("erp_work_orders", f"erp_wo_{day:%Y%m%d}.csv", header, day_rows)

    def coater_runs(self) -> None:
        rows: dict[date, list[list]] = defaultdict(list)
        for run in (r for r in self.plant.runs if r.step == "coat"):
            line, op = self.equipment(run.equipment), self.badge(run.operator)
            start = _iso_offset(run.start)
            done = run.end <= self.plant.horizon
            if not done or run.end.date() != run.start.date():
                rows[run.start.date()].append(
                    [run.run_id, run.wo_id, line, op, start, "", "", "", ""]
                )
            if done:
                rows[run.end.date()].append(
                    [
                        run.run_id,
                        run.wo_id,
                        line,
                        op,
                        start,
                        _iso_offset(run.end),
                        run.good_ft,
                        run.scrap_ft,
                        round(run.produced_ft / run.running_min, 1),
                    ]
                )
        header = [
            "run_id",
            "work_order",
            "line",
            "operator",
            "start_time",
            "end_time",
            "good_ft",
            "scrap_ft",
            "avg_fpm",
        ]
        for day, day_rows in sorted(self.spread(rows).items()):
            self.write_csv("coater_runs", f"coater_runs_{day:%Y-%m-%d}.csv", header, day_rows)

    def slitter_runs(self) -> None:
        rows: dict[date, list[list]] = defaultdict(list)
        fmt = "%m/%d/%Y %H:%M:%S"
        for run in (r for r in self.plant.runs if r.step == "slit"):
            if run.end > self.plant.horizon:
                continue  # this vendor only exports finished jobs
            rows[run.end.date()].append(
                [
                    run.run_id,
                    run.wo_id,
                    self.equipment(run.equipment),
                    self.badge(run.operator),
                    run.start.strftime(fmt),
                    run.end.strftime(fmt),
                    round(run.good_ft * FT_TO_M, 1),
                    round(run.scrap_ft * FT_TO_M, 1),
                    round(run.produced_ft * FT_TO_M / run.running_min, 1),
                ]
            )
        header = [
            "JobNo",
            "WO",
            "Machine",
            "Operator",
            "Start",
            "Stop",
            "Meters_Good",
            "Meters_Scrap",
            "Speed_mpm",
        ]
        for day, day_rows in sorted(self.spread(rows).items()):
            self.write_csv("slitter_runs", f"SLIT_{day:%Y%m%d}.csv", header, day_rows)

    def hmi_code(self, d: Downtime) -> str | None:
        reason = catalog.REASON_BY_CODE[d.reason]
        if reason.planned:
            right = self.rng.random() < self.mess.hmi_planned_right
            return reason.hmi_code if right else OTHER_CODE
        r = self.rng.random()
        if r < self.mess.hmi_right:
            return reason.hmi_code
        if r < self.mess.hmi_right + self.mess.hmi_other:
            return OTHER_CODE
        if r < self.mess.hmi_right + self.mess.hmi_other + self.mess.hmi_blank:
            return None
        return self.rng.choice([x.hmi_code for x in catalog.REASONS if x.code != d.reason])

    def hmi_downtime(self) -> None:
        by_day: dict[date, list[list]] = defaultdict(list)
        # Mid-range HMI software update: the version field changes, the schema doesn't.
        upgrade = datetime.combine(
            self.plant.start + (self.plant.end - self.plant.start) / 2, datetime.min.time()
        )
        for d in self.plant.downtime:
            if d.end > self.plant.horizon:
                continue  # the HMI logs a stop when the line restarts
            code = self.hmi_code(d)
            event = {
                "event_id": d.event_id,
                "asset": self.equipment(d.equipment),
                "ts_start": _epoch_ms(d.start),
                "duration_s": round((d.end - d.start).total_seconds()),
                "reason": None if code is None else {"code": code[:2], "text": code[3:]},
                "note": d.note,
                "operator": {"badge": self.badge(d.operator), "shift": d.shift},
                "run_ref": d.run_id,
                "hmi_ver": "4.2.1" if d.end < upgrade else "4.3.0",
            }
            by_day[d.end.date()].append([json.dumps(event)])
        for day, lines in sorted(self.spread(by_day).items()):
            path = self.root / "raw" / "hmi_downtime" / f"hmi_events_{day:%Y-%m-%d}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(line[0] + "\n" for line in lines))
            self.counts["hmi_downtime"] += len(lines)

    def qc_defects(self) -> None:
        rows: dict[date, list[list]] = defaultdict(list)
        for q in self.plant.defects:
            if q.logged_at >= self.plant.horizon:
                continue
            spelling = self.rng.choice(catalog.DEFECT_SPELLINGS[q.defect_type])
            rows[q.logged_at.date()].append(
                [
                    q.defect_id,
                    q.run_id,
                    spelling,
                    q.footage,
                    q.logged_at.strftime("%Y-%m-%d %H:%M"),
                    f"QC{self.rng.randint(1, 4)}",
                ]
            )
        header = ["defect_id", "run_id", "defect_type", "footage", "logged_at", "inspector"]
        for day, day_rows in sorted(self.spread(rows).items()):
            self.write_csv("qc_defects", f"qc_{day:%Y-%m-%d}.csv", header, day_rows)

    def schedule(self) -> None:
        rows: dict[date, list[list]] = defaultdict(list)
        for p in self.plant.plan:
            if p.planned_at < self.plant.horizon:
                rows[p.planned_at.date()].append(
                    [
                        p.wo_id,
                        p.step,
                        p.equipment,
                        p.planned_start.strftime("%Y-%m-%dT%H:%M"),
                        p.planned_end.strftime("%Y-%m-%dT%H:%M"),
                    ]
                )
        header = ["wo_id", "step", "machine", "planned_start", "planned_end"]
        for day, day_rows in sorted(rows.items()):  # the planner's export has no quirks
            self.write_csv("schedule", f"plan_{day:%Y-%m-%d}.csv", header, day_rows)


def _write_table(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _reference(root: Path) -> None:
    ref = root / "reference"
    _write_table(
        ref / "products.csv",
        ["sku", "face_code", "face", "adhesive_code", "adhesive", "width_in", "target_fpm"],
        [
            [
                p.sku,
                p.face,
                catalog.FACES[p.face],
                p.adhesive,
                catalog.ADHESIVES[p.adhesive],
                p.width_in,
                p.target_fpm,
            ]
            for p in catalog.PRODUCTS
        ],
    )
    _write_table(
        ref / "equipment.csv",
        ["code", "kind", "max_width_in", "aliases"],
        [[e.code, e.kind, e.max_width_in, "|".join(e.aliases)] for e in catalog.EQUIPMENT],
    )
    _write_table(
        ref / "operators.csv", ["badge", "area", "shift"], [list(o) for o in catalog.OPERATORS]
    )
    _write_table(
        ref / "downtime_codes.csv",
        ["hmi_code", "hmi_text", "reason", "planned"],
        [[r.hmi_code[:2], r.hmi_code[3:], r.code, r.planned] for r in catalog.REASONS]
        + [[OTHER_CODE[:2], OTHER_CODE[3:], "", ""]],
    )


def _complete(run: Run, horizon: datetime) -> bool:
    return run.end <= horizon


def _truth(root: Path, plant: Plant) -> dict:
    truth = root / "truth"
    h = plant.horizon
    stops = [d for d in plant.downtime if d.end <= h]
    _write_table(
        truth / "downtime_truth.csv",
        ["event_id", "equipment", "run_id", "reason", "planned", "start", "end", "minutes"],
        [
            [
                d.event_id,
                d.equipment,
                d.run_id or "",
                d.reason,
                catalog.REASON_BY_CODE[d.reason].planned,
                d.start.isoformat(),
                d.end.isoformat(),
                round(d.minutes, 2),
            ]
            for d in stops
        ],
    )
    _write_table(
        truth / "runs_truth.csv",
        [
            "run_id",
            "wo_id",
            "step",
            "equipment",
            "operator",
            "start",
            "end",
            "produced_ft",
            "good_ft",
            "scrap_ft",
            "running_min",
            "complete",
        ],
        [
            [
                r.run_id,
                r.wo_id,
                r.step,
                r.equipment,
                r.operator,
                r.start.isoformat(),
                r.end.isoformat(),
                r.produced_ft,
                r.good_ft,
                r.scrap_ft,
                round(r.running_min, 2),
                _complete(r, h),
            ]
            for r in plant.runs
        ],
    )

    hours = open_hours(plant.start, plant.end)
    per_eq = {}
    for eq in catalog.EQUIPMENT:
        runs = [r for r in plant.runs if r.equipment == eq.code and _complete(r, h)]
        eq_stops = [d for d in stops if d.equipment == eq.code]
        by_reason: dict[str, float] = defaultdict(float)
        for d in eq_stops:
            by_reason[d.reason] += d.minutes
        per_eq[eq.code] = {
            "runs": len(runs),
            "running_hours": round(sum(r.running_min for r in runs) / 60, 2),
            "produced_ft": sum(r.produced_ft for r in runs),
            "good_ft": sum(r.good_ft for r in runs),
            "scrap_ft": sum(r.scrap_ft for r in runs),
            "downtime_min_by_reason": {k: round(v, 1) for k, v in sorted(by_reason.items())},
        }
    summary = {
        "start": plant.start.isoformat(),
        "end_exclusive": plant.end.isoformat(),
        "open_hours_per_machine": hours,
        "work_orders": len(plant.work_orders),
        "cancelled": sum(w.cancelled for w in plant.work_orders),
        "completed_orders": len(
            {r.wo_id for r in plant.runs if r.step == "slit" and _complete(r, h)}
        ),
        "equipment": per_eq,
    }
    (truth / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def export(plant: Plant, out: Path, mess: Mess | None = None, seed: int = 0) -> dict[str, int]:
    """Write everything under out/. Returns row counts per raw source."""
    w = _Writer(out, plant, mess or Mess(), seed)
    w.erp_work_orders()
    w.coater_runs()
    w.slitter_runs()
    w.hmi_downtime()
    w.qc_defects()
    w.schedule()
    _reference(out)
    _truth(out, plant)
    return dict(w.counts)
