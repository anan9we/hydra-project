"""South African Triage Scale (SATS), adult: TEWS scoring + clinical discriminators.

Pure functions, no I/O, no AI. The agent may NOT invent a score or a colour: it comes from here.

TEWS bands cross-checked against the published adult TEWS table (Annali Italiani di Chirurgia,
SATS/TEWS Table I) and colour thresholds (0-2 Green, 3-4 Yellow, 5-6 Orange, >=7 Red).
The discriminator catalogue and the target times below are a TRIMMED subset written from the
published SATS and MUST be checked by a clinician against the official SATS poster/manual
(emssa.org.za) before any real-world use. This is a hackathon prototype on synthetic data.
"""
import re

COLOURS = ["Green", "Yellow", "Orange", "Red"]  # ascending urgency
RANK = {c: i for i, c in enumerate(COLOURS)}
ADULT_MIN_AGE = 12  # younger patients need the paediatric SATS: out of scope here, escalate to a human

# Re-check / target time to clinician review in minutes (0 = immediately). Verify against SATS.
RECHECK_MINUTES = {"Red": 0, "Orange": 10, "Yellow": 60, "Green": 240}

FIELDS = {  # field -> (entry short code, label)
    "mobility": ("mob", "Mobility"),
    "resp_rate": ("rr", "Respiratory rate (/min)"),
    "heart_rate": ("hr", "Heart rate (/min)"),
    "sbp": ("sbp", "Systolic BP (mmHg)"),
    "temp_c": ("temp", "Temperature (C)"),
    "avpu": ("avpu", "AVPU"),
    "trauma": ("trauma", "Trauma"),
}
CODE_TO_FIELD = {code: f for f, (code, _) in FIELDS.items()}


def _band(value, bands):
    for lo, hi, pts in bands:
        if (lo is None or value >= lo) and (hi is None or value <= hi):
            return pts
    raise ValueError(f"value {value} outside all bands")


# (low, high, points); integers except temperature
_RR = [(None, 8, 2), (9, 14, 0), (15, 20, 1), (21, 29, 2), (30, None, 3)]
_HR = [(None, 40, 2), (41, 50, 1), (51, 100, 0), (101, 110, 1), (111, 129, 2), (130, None, 3)]
_SBP = [(None, 70, 3), (71, 80, 2), (81, 100, 1), (101, 199, 0), (200, None, 2)]
_AVPU = {"A": 0, "V": 1, "P": 2, "U": 3}
_MOB = {"walking": 0, "with_help": 1, "immobile": 2}
_TRAUMA = {"no": 0, "yes": 1}


def score_field(field: str, value) -> int:
    if field == "resp_rate":
        return _band(int(float(value)), _RR)
    if field == "heart_rate":
        return _band(int(float(value)), _HR)
    if field == "sbp":
        return _band(int(float(value)), _SBP)
    if field == "temp_c":
        t = float(value)
        return 2 if (t < 35.0 or t > 38.4) else 0
    if field == "avpu":
        return _AVPU[str(value).strip().upper()[:1]]
    if field == "mobility":
        return _MOB[str(value).strip().lower()]
    if field == "trauma":
        return _TRAUMA[str(value).strip().lower()]
    raise KeyError(field)


def colour_for_tews(total: int) -> str:
    if total >= 7:
        return "Red"
    if total >= 5:
        return "Orange"
    if total >= 3:
        return "Yellow"
    return "Green"


def compute_tews(vitals: dict) -> dict:
    """vitals: field -> value (only fields present). Returns components, total, colour (None if incomplete)."""
    components, missing, invalid = {}, [], {}
    for f in FIELDS:
        if f not in vitals or vitals[f] in (None, ""):
            missing.append(f)
            continue
        try:
            components[f] = {"value": vitals[f], "points": score_field(f, vitals[f])}
        except (ValueError, KeyError, TypeError):
            invalid[f] = vitals[f]
            missing.append(f)
    total = sum(c["points"] for c in components.values())
    complete = not missing
    return {
        "components": components,
        "missing": missing,
        "invalid": invalid,
        "complete": complete,
        "partial_total": total,
        "total": total if complete else None,
        "colour": colour_for_tews(total) if complete else None,
    }


# ---- Clinical discriminators (trimmed subset). tier -> colour -----------------------------
TIER_COLOUR = {"emergency": "Red", "very_urgent": "Orange", "urgent": "Yellow"}

# id: (tier, label, keywords en/sw used ONLY as hints; the agent still has to quote the entry)
DISCRIMINATORS = {
    "airway_compromised": ("emergency", "Airway compromised", ["airway", "choking", "cannot breathe at all", "anakabwa"]),
    "not_breathing": ("emergency", "Not breathing / respiratory arrest", ["not breathing", "stopped breathing", "hapumui"]),
    "seizure_current": ("emergency", "Seizure - current", ["seizing", "having a fit", "convulsing", "anashikwa na kifafa sasa"]),
    "burn_facial_inhalation": ("emergency", "Burn - facial / inhalation", ["facial burn", "inhaled smoke", "smoke inhalation"]),
    "hypoglycaemia": ("emergency", "Hypoglycaemia (glucose < 3 mmol/L)", ["hypoglyc", "glucose 2", "sugar very low", "sukari ya chini"]),
    "cardiac_arrest": ("emergency", "Cardiac arrest", ["cardiac arrest", "no pulse", "moyo umesimama"]),
    "chest_pain": ("very_urgent", "Chest pain", ["chest pain", "chest tightness", "maumivu ya kifua", "kifua kinabana"]),
    "shortness_of_breath": ("very_urgent", "Shortness of breath", ["short of breath", "shortness of breath", "difficulty breathing", "kukosa pumzi", "kupumua kwa shida"]),
    "coughing_blood": ("very_urgent", "Coughing blood", ["coughing blood", "coughing up blood", "kikohozi cha damu", "anakohoa damu"]),
    "vomiting_blood": ("very_urgent", "Vomiting blood", ["vomiting blood", "kutapika damu"]),
    "seizure_postictal": ("very_urgent", "Seizure - post-ictal", ["post-ictal", "after a seizure", "after a fit", "baada ya kifafa", "degedege"]),
    "focal_neurology": ("very_urgent", "Acute focal neurology (stroke signs)", ["face droop", "one-sided weakness", "slurred speech", "arm weakness", "kiharusi"]),
    "severe_pain": ("very_urgent", "Severe pain", ["severe pain", "worst pain", "maumivu makali"]),
    "poisoning_overdose": ("very_urgent", "Poisoning / overdose", ["poison", "overdose", "swallowed pesticide", "sumu"]),
    "penetrating_torso_wound": ("very_urgent", "Stab / penetrating wound to neck, chest or abdomen", ["stabbed", "stab wound", "shot in", "amechomwa kisu"]),
    "persistent_vomiting": ("urgent", "Persistent vomiting", ["persistent vomiting", "vomiting all day", "kutapika kila mara"]),
    "abdominal_pain": ("urgent", "Abdominal pain", ["abdominal pain", "stomach pain", "belly pain", "maumivu ya tumbo"]),
    "moderate_pain": ("urgent", "Moderate pain", ["moderate pain", "maumivu ya wastani"]),
    "vaginal_bleeding": ("urgent", "Vaginal bleeding", ["vaginal bleeding", "bleeding per vagina", "kutokwa na damu ukeni"]),
    "fracture_dislocation": ("urgent", "Suspected fracture / dislocation", ["broken arm", "broken leg", "fracture", "dislocat", "amevunjika"]),
}


def discriminator_colour(did: str) -> str:
    return TIER_COLOUR[DISCRIMINATORS[did][0]]


def discriminator_catalogue_text() -> str:
    return "; ".join(f"{k} [{v[0]}]" for k, v in DISCRIMINATORS.items())


def keyword_candidates(text: str) -> list[str]:
    """Hint only: discriminator ids whose keywords appear in `text`. The agent must still quote it."""
    t = text.lower()
    return [d for d, (_, _, kws) in DISCRIMINATORS.items() if any(k in t for k in kws)]


# ---- Referral departments ------------------------------------------------------------------
DEPARTMENTS = {
    "resus_emergency": "Resuscitation / Emergency room",
    "general_opd": "General medical OPD",
    "surgical": "Surgical clinic / casualty surgery",
    "maternity_anc": "Maternity / antenatal clinic",
    "paediatrics": "Paediatric ward / clinic",
    "tb_hiv_ccc": "TB / HIV comprehensive care clinic",
    "mental_health": "Mental health clinic",
}

# ---- Text guard: the agent never diagnoses or prescribes -----------------------------------
_FORBIDDEN = re.compile(
    r"(diagnos\w*|prescri\w*|suspect\w*|likely (has|have|a)|probably (has|have|a)|suffering from|"
    r"rule[ -]?out|consistent with|start (treatment|antibiotics)|treat(ed)? with|administer\w*|"
    r"\b\d+(\.\d+)?\s?(mg|mcg|ml|iu)\b)",
    re.IGNORECASE,
)


def guard_text(text: str) -> str:
    """Raise ValueError if free text reads like a diagnosis or prescription."""
    m = _FORBIDDEN.search(text or "")
    if m:
        raise ValueError(
            f"Rejected: '{m.group(0)}' reads like a diagnosis or prescription. The Triage Desk may only "
            "describe observed vitals/symptoms and urgency, never diagnose or prescribe. Rewrite and retry."
        )
    return text
