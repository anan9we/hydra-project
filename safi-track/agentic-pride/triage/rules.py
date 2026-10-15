"""Deterministic, auditable triage scoring.

An adult early-warning score modelled on the South African Triage Scale (SATS)
TEWS table, plus emergency / very-urgent discriminators.

IMPORTANT
- This is a DEMONSTRATION implementation for a hackathon, run on SYNTHETIC data.
  Thresholds were transcribed by the authors: verify them against the official
  SATS manual and your facility's protocol before any real-world use.
- The LLM never invents a priority. It only passes measured values to this
  module; every point in the score is traced back to a value (see `evidence`).
- The agent PROPOSES a band. A named clinician must confirm it (see db.py).
"""
from dataclasses import dataclass, field
from typing import Optional

BANDS = ("GREEN", "YELLOW", "ORANGE", "RED")  # ascending urgency
# SATS target time-to-clinician, in minutes (verify against your protocol)
TARGET_MINUTES = {"RED": 0, "ORANGE": 10, "YELLOW": 60, "GREEN": 240}
ADULT_MIN_AGE = 12  # adult scale only; younger patients go straight to a clinician

EMERGENCY_SIGNS = (
    "airway_compromised",
    "not_breathing",
    "active_seizure",
    "uncontrolled_haemorrhage",
    "facial_burn_or_inhalation",
)
# Illustrative subset of SATS "very urgent" discriminators. Replace with your list.
VERY_URGENT_SIGNS = (
    "chest_pain",
    "shortness_of_breath",
    "severe_pain",
    "focal_neuro_deficit",
    "post_seizure",
    "poisoning_overdose",
    "controlled_haemorrhage",
    "pregnancy_bleeding",
)
AVPU = {"A": 0, "V": 1, "P": 2, "U": 3}
MOBILITY = {"walking": 0, "with_help": 1, "immobile": 2}

# Local rule (not part of SATS TEWS): low oxygen saturation raises urgency.
LOCAL_SPO2_ORANGE_BELOW = 92

REQUIRED = ("rr", "hr", "sbp", "temp_c", "avpu", "mobility")
PLAUSIBLE = {  # guards against unit mix-ups (e.g. Fahrenheit) and typos
    "rr": (4, 80),
    "hr": (20, 260),
    "sbp": (30, 300),
    "temp_c": (30.0, 44.0),
    "spo2": (50, 100),
}
LABELS = {
    "rr": "RR",
    "hr": "HR",
    "sbp": "SBP",
    "temp_c": "Temp",
    "avpu": "AVPU",
    "mobility": "Mobility",
    "trauma": "Trauma",
    "spo2": "SpO2",
}
UNITS = {"rr": "/min", "hr": "/min", "sbp": " mmHg", "temp_c": " C", "spo2": "%"}


@dataclass
class Vitals:
    rr: Optional[float] = None
    hr: Optional[float] = None
    sbp: Optional[float] = None
    temp_c: Optional[float] = None
    spo2: Optional[float] = None
    avpu: Optional[str] = None
    mobility: Optional[str] = None
    trauma: bool = False

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in LABELS}


@dataclass
class Score:
    tews: int
    tews_band: str
    band: str
    components: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    emergency_signs: list = field(default_factory=list)
    very_urgent_signs: list = field(default_factory=list)


def band_index(band: str) -> int:
    return BANDS.index(band)


def _fmt(x) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


# ---- TEWS point tables: each returns (points, range label) --------------------
def _rr(v):
    if v < 9:
        return 2, "<9"
    if v <= 14:
        return 0, "9-14"
    if v <= 20:
        return 1, "15-20"
    if v <= 29:
        return 2, "21-29"
    return 3, ">29"


def _hr(v):
    if v < 41:
        return 2, "<41"
    if v <= 50:
        return 1, "41-50"
    if v <= 100:
        return 0, "51-100"
    if v <= 110:
        return 1, "101-110"
    if v <= 129:
        return 2, "111-129"
    return 3, ">129"


def _sbp(v):
    if v < 71:
        return 3, "<71"
    if v <= 80:
        return 2, "71-80"
    if v <= 100:
        return 1, "81-100"
    if v <= 199:
        return 0, "101-199"
    return 2, ">199"


def _temp(v):
    if v < 35:
        return 2, "<35"
    if v > 38.4:
        return 2, ">38.4"
    return 0, "35-38.4"


# ---- validation -----------------------------------------------------------------
def validate(v: Vitals) -> list:
    """Return a list of human-readable problems (empty list = fine)."""
    errs = []
    for name, (lo, hi) in PLAUSIBLE.items():
        val = getattr(v, name)
        if val is not None and not (lo <= val <= hi):
            hint = " If this is Fahrenheit, convert to Celsius first." if name == "temp_c" else ""
            errs.append(f"{name}={_fmt(val)} is outside the plausible range {lo}-{hi}.{hint}")
    if v.avpu is not None and v.avpu not in AVPU:
        errs.append(f"avpu='{v.avpu}' must be one of A, V, P, U.")
    if v.mobility is not None and v.mobility not in MOBILITY:
        errs.append(f"mobility='{v.mobility}' must be one of {list(MOBILITY)}.")
    return errs


def missing(v: Vitals) -> list:
    return [k for k in REQUIRED if getattr(v, k) is None]


# ---- scoring --------------------------------------------------------------------
def score(v: Vitals, emergency_signs=(), very_urgent_signs=()) -> Score:
    comps = {}

    def add(name, value, pts, rng):
        comps[name] = {"value": value, "points": pts, "range": rng}

    for name, fn in (("rr", _rr), ("hr", _hr), ("sbp", _sbp), ("temp_c", _temp)):
        val = getattr(v, name)
        if val is not None:
            pts, rng = fn(val)
            add(name, val, pts, rng)
    if v.avpu is not None:
        add("avpu", v.avpu, AVPU[v.avpu], v.avpu)
    if v.mobility is not None:
        add("mobility", v.mobility, MOBILITY[v.mobility], v.mobility)
    add("trauma", bool(v.trauma), 1 if v.trauma else 0, "yes" if v.trauma else "no")

    tews = sum(c["points"] for c in comps.values())
    tews_band = "GREEN" if tews <= 2 else "YELLOW" if tews <= 4 else "ORANGE" if tews <= 6 else "RED"
    band = tews_band

    evidence = []
    for name, c in comps.items():
        if c["points"] > 0:
            if name == "trauma":
                evidence.append("Trauma: yes -> +1 TEWS")
            else:
                unit = UNITS.get(name, "")
                evidence.append(
                    f"{LABELS[name]} {_fmt(c['value'])}{unit} -> +{c['points']} TEWS ({c['range']})"
                )
    if tews == 0:
        evidence.append("All scored vitals are in the normal range (TEWS 0)")
    evidence.append(f"TEWS total {tews} -> {tews_band}")

    for s in emergency_signs:
        band = "RED"
        evidence.append(f"Emergency sign '{s}' reported -> RED")
    for s in very_urgent_signs:
        if band_index(band) < band_index("ORANGE"):
            band = "ORANGE"
        evidence.append(f"Discriminator '{s}' reported -> at least ORANGE")
    if v.spo2 is not None and v.spo2 < LOCAL_SPO2_ORANGE_BELOW:
        if band_index(band) < band_index("ORANGE"):
            band = "ORANGE"
        evidence.append(
            f"SpO2 {_fmt(v.spo2)}% is below {LOCAL_SPO2_ORANGE_BELOW}% (local rule) -> at least ORANGE"
        )
    if band != tews_band:
        evidence.append(f"Final band {band} (TEWS alone gave {tews_band})")

    return Score(
        tews=tews,
        tews_band=tews_band,
        band=band,
        components=comps,
        evidence=evidence,
        emergency_signs=list(emergency_signs),
        very_urgent_signs=list(very_urgent_signs),
    )


# ---- deterioration --------------------------------------------------------------
def compare(prev: dict, new: Score):
    """Compare a stored previous observation with a new Score.

    `prev` needs: tews, band, components, emergency_signs.
    Returns (flag, reasons). Reasons always cite old and new values.
    A flag is raised only on MEASURED change: TEWS up 2+, band up, or a new emergency sign.
    """
    reasons = []
    d = new.tews - prev["tews"]
    rise = d >= 2
    if rise:
        reasons.append(f"TEWS rose {prev['tews']} -> {new.tews} (+{d})")
    band_up = band_index(new.band) > band_index(prev["band"])
    if band_up:
        reasons.append(f"Band rose {prev['band']} -> {new.band}")
    new_em = [s for s in new.emergency_signs if s not in prev.get("emergency_signs", [])]
    for s in new_em:
        reasons.append(f"New emergency sign '{s}'")
    flag = rise or band_up or bool(new_em)
    if flag:
        for name, c in new.components.items():
            old = prev["components"].get(name)
            old_pts = old["points"] if old else 0
            if c["points"] > old_pts:
                unit = UNITS.get(name, "")
                old_val = _fmt(old["value"]) if old else "not recorded"
                reasons.append(
                    f"{LABELS[name]} {old_val}{unit if old else ''} -> {_fmt(c['value'])}{unit} "
                    f"(+{c['points'] - old_pts} TEWS)"
                )
    else:
        reasons.append(
            f"TEWS {prev['tews']} -> {new.tews}: no rise of 2+ points, band unchanged ({new.band}); no flag"
        )
    return flag, reasons
