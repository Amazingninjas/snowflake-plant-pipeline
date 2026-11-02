"""Reference data for the synthetic plant.

Everything here is invented. Product codes, customers, operators and equipment are
generic and don't describe any real company.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    face: str
    adhesive: str
    width_in: int  # master-roll web width
    target_fpm: int  # coater target line speed, feet per minute


@dataclass(frozen=True)
class Equipment:
    code: str  # canonical id used from silver onward
    kind: str  # "coater" or "slitter"
    max_width_in: int
    speed_factor: float  # coaters: share of the product's target speed they reach
    base_fpm: int  # slitters: running speed; coaters: 0 (speed comes from the product)
    stops_per_hr: float  # mean unplanned stops per running hour
    aliases: tuple[str, ...]  # how the source systems spell it


@dataclass(frozen=True)
class Reason:
    code: str  # canonical downtime reason (the label the AI step should recover)
    hmi_code: str  # what the HMI menu calls it
    planned: bool
    mean_min: float  # mean duration; durations are lognormal around it
    weight_coater: float  # relative frequency among unplanned coater stops
    weight_slitter: float
    notes: tuple[str, ...]  # what operators type, before messing up


FACES = {
    "PET2C": "2 mil clear polyester",
    "PET3W": "3 mil white polyester",
    "PP26": "2.6 mil polypropylene",
    "PI1": "1 mil polyimide",
    "AL2": "2 mil aluminum foil",
}

ADHESIVES = {
    "AP": "acrylic, permanent",
    "AR": "acrylic, removable",
    "HM": "rubber hot-melt",
    "SI": "silicone",
}

PRODUCTS = (
    Product("PSM-PET2C-AP-24", "PET2C", "AP", 24, 260),
    Product("PSM-PET2C-AP-54", "PET2C", "AP", 54, 240),
    Product("PSM-PET3W-AP-30", "PET3W", "AP", 30, 250),
    Product("PSM-PET3W-AR-30", "PET3W", "AR", 30, 230),
    Product("PSM-PET3W-HM-60", "PET3W", "HM", 60, 380),
    Product("PSM-PP26-HM-24", "PP26", "HM", 24, 400),
    Product("PSM-PP26-HM-48", "PP26", "HM", 48, 380),
    Product("PSM-PP26-AR-30", "PP26", "AR", 30, 240),
    Product("PSM-PI1-SI-24", "PI1", "SI", 24, 120),
    Product("PSM-PI1-AP-30", "PI1", "AP", 30, 140),
    Product("PSM-AL2-AP-24", "AL2", "AP", 24, 180),
    Product("PSM-AL2-AP-48", "AL2", "AP", 48, 170),
)

EQUIPMENT = (
    Equipment("C1", "coater", 60, 1.00, 0, 0.35, ("C1", "Coater 1", "COATER-01", "coater1")),
    Equipment("C2", "coater", 30, 0.85, 0, 0.50, ("C2", "Coater 2", "COATER-02", "coater2")),
    Equipment("S1", "slitter", 60, 1.00, 320, 0.40, ("S1", "Slitter 1", "SLT-01", "slitter #1")),
    Equipment("S2", "slitter", 60, 1.00, 260, 0.50, ("S2", "Slitter 2", "SLT-02", "slitter #2")),
)

# Downtime taxonomy. The HMI offers a menu, but most operators pick OTHER or nothing and
# describe the stop in the free-text note instead; see export.HMI_CODE_* rates.
REASONS = (
    Reason(
        "WEB_BREAK",
        "10 WEB BREAK",
        False,
        18,
        18,
        15,
        (
            "web break at unwind",
            "web brk @ rewind, rethreaded",
            "broke web going into oven",
            "web snapped at nip, rethread",
            "wb @ unwind - tension spike",
            "web broke again zone 2",
        ),
    ),
    Reason(
        "SPLICE_FAILURE",
        "20 SPLICE",
        False,
        12,
        12,
        0,
        (
            "splice came apart",
            "bad splice on new liner roll",
            "splice failed at flying splicer",
            "splice pulled apart going thru oven",
            "resplice - tape didnt hold",
            "auto splice miss",
        ),
    ),
    Reason(
        "ADHESIVE_SYSTEM",
        "30 ADHESIVE",
        False,
        35,
        14,
        0,
        (
            "adh pump clogged",
            "adhesive viscosity off, waiting on lab",
            "filter change on adh line",
            "glue pump lost prime",
            "adhesive tote empty, swapping",
            "air in adh line, bled it",
        ),
    ),
    Reason(
        "COATING_ADJUST",
        "40 COAT QUALITY",
        False,
        15,
        20,
        0,
        (
            "streaks across web, adjusting die",
            "coat weight low, gap adjust",
            "lines in coating, cleaned die lip",
            "coat wt out of spec - adjusting",
            "chatter marks, slowed + adjusted",
            "edge bead heavy, adjusting deckles",
        ),
    ),
    Reason(
        "BLADE_CHANGE",
        "45 KNIVES",
        False,
        15,
        0,
        25,
        (
            "blade change",
            "dull knives, changed",
            "new blades",
            "knife change - fuzzy edges",
            "changed bottom knives",
            "blade chipped",
        ),
    ),
    Reason(
        "MATERIAL_SHORTAGE",
        "50 MATERIAL",
        False,
        40,
        8,
        15,
        (
            "waiting on liner",
            "no cores",
            "out of face stock - waiting on warehouse",
            "core shortage 3in",
            "waiting for material from stores",
            "liner roll not staged",
        ),
    ),
    Reason(
        "QC_HOLD",
        "60 QC HOLD",
        False,
        25,
        10,
        10,
        (
            "waiting on QC",
            "QC hold - peel test",
            "holding for lab approval",
            "waiting on qc to sign off first roll",
            "lab checking coat wt",
            "QA hold on roll 2",
        ),
    ),
    Reason(
        "MECHANICAL",
        "70 MECHANICAL",
        False,
        60,
        7,
        12,
        (
            "bearing noise on idler, maint looking",
            "drive belt slipping",
            "nip roll not closing",
            "tension arm stuck",
            "rewind shaft wont lock",
            "chain off on unwind",
        ),
    ),
    Reason(
        "CONTROLS",
        "80 ELECTRICAL",
        False,
        10,
        7,
        10,
        (
            "PLC fault, reset",
            "drive fault E-14",
            "e-stop tripped, reset",
            "web guide sensor lost",
            "HMI froze, rebooted",
            "oven temp alarm, sensor",
        ),
    ),
    Reason(
        "STAFFING",
        "85 NO OPERATOR",
        False,
        30,
        4,
        8,
        (
            "no operator",
            "short handed, waiting on relief",
            "operator pulled to other line",
            "waiting on 2nd person",
            "break coverage",
            "op at training",
        ),
    ),
    Reason(
        "CHANGEOVER",
        "90 CHANGEOVER",
        True,
        40,
        0,
        0,
        (
            "changeover",
            "product change",
            "roll change + new setup",
            "setup for next order",
            "cleanup and changeover",
            "c/o to next job",
        ),
    ),
    Reason(
        "CLEANING",
        "91 CLEANING",
        True,
        30,
        0,
        0,
        (
            "cleaning rolls",
            "wash up",
            "clean die",
            "end of run cleanup",
            "wipe down nips",
            "cleaning oven",
        ),
    ),
    Reason(
        "PLANNED_MAINTENANCE",
        "95 PM",
        True,
        120,
        0,
        0,
        (
            "PM",
            "scheduled maint",
            "planned maintenance",
            "PM - lube + inspect",
            "maint PM per schedule",
            "monthly PM",
        ),
    ),
)

REASON_BY_CODE = {r.code: r for r in REASONS}

# QC defect types and how inspectors spell them in the QC log.
DEFECT_SPELLINGS = {
    "STARTUP_WASTE": ("Startup waste", "startup", "START-UP WASTE"),
    "PINHOLES": ("Pinholes", "pinhole", "PIN HOLES"),
    "STREAKS": ("Streaks", "streaking", "STREAK"),
    "COAT_WEIGHT": ("Coat weight", "coat wt", "CW OUT OF SPEC"),
    "WRINKLE": ("Wrinkle", "wrinkles", "WRINKLED"),
    "SPLICE_WASTE": ("Splice waste", "splice", "SPLICE"),
    "CONTAMINATION": ("Contamination", "contam", "DIRT/GELS"),
    "ADHESIVE_VOIDS": ("Adhesive voids", "voids", "ADH VOIDS"),
    "EDGE_FUZZ": ("Edge fuzz", "fuzzy edges", "EDGE QUALITY"),
    "TELESCOPING": ("Telescoping", "telescoped", "TELESCOPE"),
    "CORE_CRUSH": ("Core crush", "crushed core", "CORE DAMAGE"),
}

# Plain-language company names built from word lists; any match to a real business is chance.
_CUSTOMER_FIRST = (
    "Granite",
    "Northfield",
    "Bayside",
    "Summit",
    "Pioneer",
    "Cedar",
    "Ironwood",
    "Harbor",
    "Blue Ridge",
    "Keystone",
    "Maple Hill",
    "Riverbend",
)
_CUSTOMER_SECOND = (
    "Labels",
    "Electronics",
    "Packaging",
    "Graphics",
    "Converting",
    "Industries",
    "Signs",
    "Automotive",
)


def customers(n: int = 25) -> list[tuple[str, str]]:
    """(customer number, name) pairs, deterministic."""
    out = []
    for i in range(n):
        first = _CUSTOMER_FIRST[i % len(_CUSTOMER_FIRST)]
        second = _CUSTOMER_SECOND[(i * 5 + i // len(_CUSTOMER_FIRST)) % len(_CUSTOMER_SECOND)]
        out.append((f"{10100 + i * 7}", f"{first} {second}"))
    return out


# 3 shifts x 2 areas x 3 operators. Badge ids are what the source systems record.
SHIFTS = ("A", "B", "C")  # A 06-14, B 14-22, C 22-06
OPERATORS = tuple(
    (f"OP{area_i * 9 + shift_i * 3 + k + 1:03d}", area, shift)
    for area_i, area in enumerate(("coater", "slitter"))
    for shift_i, shift in enumerate(SHIFTS)
    for k in range(3)
)
