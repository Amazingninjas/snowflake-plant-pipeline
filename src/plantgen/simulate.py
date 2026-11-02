"""Simulate the plant: plan work orders onto machines, then run them with downtime.

Every time here is a naive local wall-clock datetime (America/New_York). export.py decides
how each source system formats it. The plant runs 24/5: Monday 06:00 to Saturday 06:00.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from plantgen import catalog
from plantgen.catalog import Equipment, Product, Reason

ORDERS_PER_WEEKDAY = 5.5
ORDER_QTY_FT = (20_000, 120_000)  # finished linear feet at master-roll width
DUE_DAYS = (7, 18)
RUSH_RATE = 0.2  # orders the customer needs in a few days; FIFO planning makes these late
RUSH_DUE_DAYS = (2, 4)
READY_AFTER = timedelta(hours=24)  # material staging between release and the first possible start
SLIT_ALLOWANCE = 0.02  # extra good coated footage so slitting losses don't short the order
CURE = timedelta(hours=4)  # adhesive dwell between coating and slitting
PULL_AHEAD = timedelta(hours=2)  # how early a free machine may start a planned job
CHANGE_RATE = 0.15  # share of orders the customer revises (qty or due date)
CANCEL_RATE = 0.02
PLANNED_CHANGEOVER_MIN = {"coater": 45, "slitter": 20}  # what the planner assumes
PLANNED_SCRAP = {"coater": 0.03, "slitter": 0.01}
CLEANUP_AFTER_COAT = 0.3  # chance of an end-of-run cleaning stop


@dataclass
class WorkOrderVersion:
    modified: datetime
    qty_ft: int
    due: date
    status: str  # RELEASED, CANCELLED, IN_PROCESS, COATED, COMPLETE


@dataclass
class WorkOrder:
    wo_id: str
    customer_no: str
    customer_name: str
    product: Product
    created: datetime
    qty_ft: int  # final quantity, after any customer change
    due: date
    cancelled: bool = False
    versions: list[WorkOrderVersion] = field(default_factory=list)


@dataclass
class PlannedOp:
    wo_id: str
    step: str  # "coat" or "slit"
    equipment: str
    planned_at: datetime  # when the planner made the assignment
    planned_start: datetime
    planned_end: datetime


@dataclass
class Run:
    run_id: str
    wo_id: str
    step: str
    equipment: str
    operator: str
    start: datetime  # job started on the MES, changeover included
    end: datetime
    produced_ft: int
    scrap_ft: int
    running_min: float  # time the line was actually running

    @property
    def good_ft(self) -> int:
        return self.produced_ft - self.scrap_ft


@dataclass
class Downtime:
    event_id: str
    equipment: str
    run_id: str | None
    start: datetime
    end: datetime
    reason: str  # true canonical reason
    note: str  # what the operator typed
    operator: str
    shift: str

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class Defect:
    defect_id: str
    run_id: str
    defect_type: str  # canonical
    footage: int
    logged_at: datetime


@dataclass
class Plant:
    start: date
    end: date  # exclusive; nothing at or after this moment exists yet
    work_orders: list[WorkOrder]
    plan: list[PlannedOp]
    runs: list[Run]
    downtime: list[Downtime]
    defects: list[Defect]

    @property
    def horizon(self) -> datetime:
        return datetime.combine(self.end, time())


# --- calendar -----------------------------------------------------------------------------


def is_open(t: datetime) -> bool:
    wd = t.weekday()
    if wd == 0:
        return t.hour >= 6
    if wd <= 4:
        return True
    return wd == 5 and t.hour < 6


def next_open(t: datetime) -> datetime:
    """t if the plant is open then, else the next Monday 06:00."""
    if is_open(t):
        return t
    monday = (t + timedelta(days=(7 - t.weekday()) % 7)).replace(
        hour=6, minute=0, second=0, microsecond=0
    )
    return monday if monday > t else monday + timedelta(days=7)


def shift_of(t: datetime) -> str:
    if 6 <= t.hour < 14:
        return "A"
    return "B" if 14 <= t.hour < 22 else "C"


def open_hours(start: date, end: date) -> float:
    t, n = datetime.combine(start, time()), 0
    while t < datetime.combine(end, time()):
        n += is_open(t)
        t += timedelta(hours=1)
    return float(n)


# --- small random helpers -----------------------------------------------------------------


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _after(t: datetime, minutes: float) -> datetime:
    """t plus minutes, to the whole second (what the source systems record)."""
    return t + timedelta(seconds=round(minutes * 60))


def _minutes(rng: random.Random, mean: float, sigma: float = 0.6) -> float:
    """Lognormal duration with the given mean, at least one minute."""
    return max(1.0, rng.lognormvariate(math.log(mean) - sigma**2 / 2, sigma))


_ABBREV = {
    "waiting": "wtg",
    "adhesive": "adh",
    "operator": "op",
    "changed": "chgd",
    "again": "agn",
    "material": "matl",
    "maintenance": "maint",
}
_TAILS = (" - roll 2", " roll 3", " again", " ??", " - told sup", " (2nd time)", " ~15 min")


def _typed(rng: random.Random, base: str) -> str:
    """An operator's version of a note: abbreviations, a typo, shouting, a tail."""
    words = [_ABBREV.get(w, w) if rng.random() < 0.3 else w for w in base.split(" ")]
    text = " ".join(words)
    if rng.random() < 0.15:
        spots = [i for i in range(len(text) - 1) if text[i].isalpha() and text[i + 1].isalpha()]
        if spots:
            i = rng.choice(spots)
            text = text[:i] + text[i + 1] + text[i] + text[i + 2 :]
    r = rng.random()
    if r < 0.15:
        text = text.upper()
    elif r < 0.25:
        text = text.capitalize()
    if rng.random() < 0.2:
        text += rng.choice(_TAILS)
    return text


class _Ids:
    def __init__(self) -> None:
        self._n: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        return f"{prefix}-{self._n[prefix]:06d}"


# --- work orders and the plan -------------------------------------------------------------


def _work_orders(rng: random.Random, start: date, end: date) -> list[WorkOrder]:
    custs = catalog.customers()
    buys = {no: rng.sample(catalog.PRODUCTS, k=rng.choice((2, 3))) for no, _ in custs}
    out: list[WorkOrder] = []
    seq, d = 0, start
    while d < end:
        if d.weekday() < 5:
            for _ in range(_poisson(rng, ORDERS_PER_WEEKDAY)):
                seq += 1
                no, name = rng.choice(custs)
                created = datetime.combine(d, time(7)) + timedelta(minutes=rng.randint(0, 540))
                qty = rng.randrange(ORDER_QTY_FT[0], ORDER_QTY_FT[1] + 1, 500)
                lead = RUSH_DUE_DAYS if rng.random() < RUSH_RATE else DUE_DAYS
                due = d + timedelta(days=rng.randint(*lead))
                wo = WorkOrder(
                    f"WO-{d:%y}-{seq:05d}", no, name, rng.choice(buys[no]), created, qty, due
                )
                wo.versions.append(WorkOrderVersion(created, qty, due, "RELEASED"))
                # Customer changes and cancellations land before material is staged.
                r = rng.random()
                when = created + timedelta(minutes=rng.randint(120, 1200))
                if r < CANCEL_RATE:
                    wo.cancelled = True
                    wo.versions.append(WorkOrderVersion(when, qty, due, "CANCELLED"))
                elif r < CANCEL_RATE + CHANGE_RATE:
                    if rng.random() < 0.5:
                        wo.qty_ft = round(qty * rng.uniform(0.6, 1.5) / 500) * 500
                    else:
                        wo.due = due + timedelta(days=rng.choice((-4, -2, 3, 5, 7)))
                    wo.versions.append(WorkOrderVersion(when, wo.qty_ft, wo.due, "RELEASED"))
                out.append(wo)
        d += timedelta(days=1)
    return out


def _coat_target_ft(wo: WorkOrder) -> int:
    return round(wo.qty_ft * (1 + SLIT_ALLOWANCE))


def _plan(work_orders: list[WorkOrder], start: date) -> list[PlannedOp]:
    """First-come planning at target speed with no downtime, like a hand-built schedule."""
    coaters = [e for e in catalog.EQUIPMENT if e.kind == "coater"]
    slitters = [e for e in catalog.EQUIPMENT if e.kind == "slitter"]
    free = {e.code: next_open(datetime.combine(start, time(6))) for e in catalog.EQUIPMENT}
    plan: list[PlannedOp] = []
    for wo in sorted(work_orders, key=lambda w: w.created):
        if wo.cancelled:
            continue
        ready = wo.created + READY_AFTER
        fits = [c for c in coaters if wo.product.width_in <= c.max_width_in]
        c = min(fits, key=lambda e: max(free[e.code], ready))
        cs = next_open(max(free[c.code], ready))
        coat_min = PLANNED_CHANGEOVER_MIN["coater"] + _coat_target_ft(wo) * (
            1 + PLANNED_SCRAP["coater"]
        ) / (wo.product.target_fpm * c.speed_factor)
        ce = _after(cs, coat_min)
        free[c.code] = ce
        plan.append(PlannedOp(wo.wo_id, "coat", c.code, wo.created, cs, ce))

        s = min(slitters, key=lambda e: max(free[e.code], ce + CURE))
        ss = next_open(max(free[s.code], ce + CURE))
        se = _after(ss, PLANNED_CHANGEOVER_MIN["slitter"] + _coat_target_ft(wo) / s.base_fpm)
        free[s.code] = se
        plan.append(PlannedOp(wo.wo_id, "slit", s.code, wo.created, ss, se))
    return plan


# --- running jobs -------------------------------------------------------------------------


def _pm_windows(eq: Equipment, start: date, end: date) -> list[datetime]:
    """Scheduled PM starts: coaters every other Monday, slitters monthly."""
    out, d = [], start
    while d < end:
        if d.weekday() == 0:
            week = d.isocalendar().week
            nth_monday = (d.day - 1) // 7 + 1
            due = {
                "C1": week % 2 == 0,
                "C2": week % 2 == 1,
                "S1": nth_monday == 1,
                "S2": nth_monday == 3,
            }[eq.code]
            if due:
                out.append(datetime.combine(d, time(6)))
        d += timedelta(days=1)
    return out


def _operator(rng: random.Random, area: str, shift: str) -> str:
    return rng.choice([b for b, a, s in catalog.OPERATORS if a == area and s == shift])


def _pick_reason(rng: random.Random, kind: str) -> Reason:
    pool = [r for r in catalog.REASONS if not r.planned]
    weights = [r.weight_coater if kind == "coater" else r.weight_slitter for r in pool]
    return rng.choices(pool, weights=weights)[0]


_STOP_SCRAP = {  # unplanned stop -> (defect types it can leave behind, footage range)
    "WEB_BREAK": (("WRINKLE",), (100, 400)),
    "SPLICE_FAILURE": (("SPLICE_WASTE",), (100, 400)),
    "COATING_ADJUST": (("STREAKS", "COAT_WEIGHT"), (150, 500)),
    "BLADE_CHANGE": (("EDGE_FUZZ",), (100, 300)),
}
_RANDOM_DEFECTS = {  # kind -> (defect type, chance per run, footage range)
    "coater": (
        ("PINHOLES", 0.25, (200, 1200)),
        ("CONTAMINATION", 0.15, (100, 600)),
        ("ADHESIVE_VOIDS", 0.10, (150, 800)),
    ),
    "slitter": (
        ("TELESCOPING", 0.10, (100, 500)),
        ("CORE_CRUSH", 0.05, (50, 200)),
        ("WRINKLE", 0.10, (100, 400)),
    ),
}


class _Sim:
    def __init__(self, rng: random.Random, horizon: datetime) -> None:
        self.rng = rng
        self.horizon = horizon
        self.ids = _Ids()
        self.runs: list[Run] = []
        self.downtime: list[Downtime] = []
        self.defects: list[Defect] = []

    def stop(
        self,
        eq: Equipment,
        run: str | None,
        start: datetime,
        minutes: float,
        reason: Reason,
        run_operator: str | None,
        run_shift: str | None,
    ) -> datetime:
        shift = shift_of(start)
        op = (
            run_operator
            if run_operator and shift == run_shift
            else _operator(self.rng, eq.kind, shift)
        )
        end = _after(start, minutes)
        self.downtime.append(
            Downtime(
                self.ids.next("DT"),
                eq.code,
                run,
                start,
                end,
                reason.code,
                _typed(self.rng, self.rng.choice(reason.notes)),
                op,
                shift,
            )
        )
        return end

    def job(self, eq: Equipment, wo: WorkOrder, step: str, t0: datetime, input_ft: int) -> Run:
        """Run one job from changeover to last foot; returns the run (end = line stopped)."""
        rng = self.rng
        run_id = self.ids.next("CR" if step == "coat" else "SJ")
        shift = shift_of(t0)
        operator = _operator(rng, eq.kind, shift)
        co = catalog.REASON_BY_CODE["CHANGEOVER"]
        t = self.stop(
            eq, run_id, t0, _minutes(rng, 40 if step == "coat" else 15, 0.4), co, operator, shift
        )

        base = wo.product.target_fpm * eq.speed_factor if step == "coat" else eq.base_fpm
        speed = base * min(1.1, max(0.7, rng.gauss(1.0, 0.05)))

        defects = [
            ("STARTUP_WASTE", rng.randint(300, 800) if step == "coat" else rng.randint(50, 200))
        ]
        expected_min = input_ft / speed
        stops = []
        for _ in range(_poisson(rng, eq.stops_per_hr * expected_min / 60)):
            reason = _pick_reason(rng, eq.kind)
            stops.append((rng.random(), reason))
            if reason.code in _STOP_SCRAP:
                types, (lo, hi) = _STOP_SCRAP[reason.code]
                defects.append((rng.choice(types), rng.randint(lo, hi)))
        for dtype, chance, (lo, hi) in _RANDOM_DEFECTS[eq.kind]:
            if rng.random() < chance:
                defects.append((dtype, rng.randint(lo, hi)))
        scrap = sum(ft for _, ft in defects)
        # Coaters run until the order's good footage is made; slitters consume what was coated.
        produced = input_ft + scrap if step == "coat" else input_ft
        scrap = min(scrap, produced)
        run_min = produced / speed

        done = 0.0
        for frac, reason in sorted(stops, key=lambda s: s[0]):
            at = frac * run_min
            t = _after(t, at - done)
            t = self.stop(eq, run_id, t, _minutes(rng, reason.mean_min), reason, operator, shift)
            done = at
        end = _after(t, run_min - done)

        run = Run(run_id, wo.wo_id, step, eq.code, operator, t0, end, produced, scrap, run_min)
        self.runs.append(run)
        for dtype, ft in defects:
            logged = _after(t0, 10) if dtype == "STARTUP_WASTE" else end
            self.defects.append(
                Defect(self.ids.next("QD"), run_id, dtype, ft, _after(logged, rng.randint(0, 30)))
            )
        return run


def _execute(
    sim: _Sim, wos: dict[str, WorkOrder], plan: list[PlannedOp], start: date, end: date
) -> None:
    coat_runs: dict[str, Run] = {}
    for step, kind in (("coat", "coater"), ("slit", "slitter")):
        for eq in (e for e in catalog.EQUIPMENT if e.kind == kind):
            free = next_open(datetime.combine(start, time(6)))
            pms = _pm_windows(eq, start, end)
            ops = sorted(
                (p for p in plan if p.equipment == eq.code and p.step == step),
                key=lambda p: p.planned_start,
            )
            for op in ops:
                wo = wos[op.wo_id]
                earliest = op.planned_start - PULL_AHEAD
                if step == "slit":
                    coat = coat_runs.get(wo.wo_id)
                    if coat is None:
                        continue
                    earliest = max(earliest, coat.end + CURE)
                t = next_open(max(free, earliest))
                while pms and pms[0] <= t:  # planners leave PM off the schedule; it still happens
                    pm_start = next_open(max(pms.pop(0), free))
                    free = sim.stop(
                        eq,
                        None,
                        pm_start,
                        _minutes(sim.rng, 120, 0.3),
                        catalog.REASON_BY_CODE["PLANNED_MAINTENANCE"],
                        None,
                        None,
                    )
                    t = next_open(max(free, earliest))
                if t >= sim.horizon:
                    continue  # not started yet when the data ends
                input_ft = _coat_target_ft(wo) if step == "coat" else coat_runs[wo.wo_id].good_ft
                run = sim.job(eq, wo, step, t, input_ft)
                free = run.end
                if step == "coat":
                    coat_runs[wo.wo_id] = run
                    if sim.rng.random() < CLEANUP_AFTER_COAT:
                        free = sim.stop(
                            eq,
                            run.run_id,
                            run.end,
                            _minutes(sim.rng, 30, 0.4),
                            catalog.REASON_BY_CODE["CLEANING"],
                            run.operator,
                            shift_of(run.start),
                        )


def _status_versions(wos: dict[str, WorkOrder], runs: list[Run]) -> None:
    for run in runs:
        wo = wos[run.wo_id]
        if run.step == "coat":
            wo.versions.append(WorkOrderVersion(run.start, wo.qty_ft, wo.due, "IN_PROCESS"))
            wo.versions.append(WorkOrderVersion(run.end, wo.qty_ft, wo.due, "COATED"))
        else:
            wo.versions.append(WorkOrderVersion(run.end, wo.qty_ft, wo.due, "COMPLETE"))
    for wo in wos.values():
        wo.versions.sort(key=lambda v: v.modified)


def simulate(start: date, days: int, seed: int) -> Plant:
    rng = random.Random(seed)
    end = start + timedelta(days=days)
    work_orders = _work_orders(rng, start, end)
    wos = {w.wo_id: w for w in work_orders}
    plan = _plan(work_orders, start)
    sim = _Sim(rng, datetime.combine(end, time()))
    _execute(sim, wos, plan, start, end)
    _status_versions(wos, sim.runs)
    _renumber(sim)
    return Plant(start, end, work_orders, plan, sim.runs, sim.downtime, sim.defects)


def _renumber(sim: _Sim) -> None:
    """Machines are simulated one after another; give out ids in time order, like an MES."""
    sim.runs.sort(key=lambda r: (r.start, r.equipment))
    sim.downtime.sort(key=lambda d: (d.start, d.equipment))
    sim.defects.sort(key=lambda d: (d.logged_at, d.run_id))
    ids = _Ids()
    new_run_id = {}
    for r in sim.runs:
        new_run_id[r.run_id] = ids.next("CR" if r.step == "coat" else "SJ")
        r.run_id = new_run_id[r.run_id]
    for d in sim.downtime:
        d.event_id = ids.next("DT")
        d.run_id = new_run_id[d.run_id] if d.run_id else None
    for q in sim.defects:
        q.defect_id = ids.next("QD")
        q.run_id = new_run_id[q.run_id]
