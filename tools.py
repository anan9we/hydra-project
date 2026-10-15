"""Tool logic (plain Python, no MCP dependency, so it is unit-testable).

Rules baked in:
- Tools only create PENDING proposals. None of them can confirm a priority,
  diagnose, or prescribe. There is deliberately no 'approve' tool here.
- Every urgency claim cites the measured value behind it (see rules.score).
- Every call, success or failure, is written to the audit log with inputs,
  outputs and a timestamp.
"""
import functools
import inspect
from datetime import datetime, timedelta

from . import db, rules

DEPARTMENTS = ("resuscitation", "general_medical", "surgical", "maternity", "orthopaedics", "paediatrics")
TOOL_NAMES = ("get_queue", "score_priority", "raise_recheck_flag", "route_referral")  # agent-facing


class ToolError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def audited(fn):
    """Log inputs, outputs and timestamp of every tool call. Never lets exceptions escape."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        inputs = dict(sig.bind(*args, **kwargs).arguments)
        try:
            out = fn(*args, **kwargs)
        except ToolError as e:
            out = {"ok": False, "error": e.code, "message": e.message, **e.extra}
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": "internal_error", "message": f"{type(e).__name__}: {e}"}
        db.log_audit("agent", fn.__name__, inputs, out, bool(out.get("ok", True)))
        return out

    return wrapper


# ---- helpers ----------------------------------------------------------------------
def _signs(values, allowed, label):
    vals = sorted({str(s).strip().lower() for s in (values or []) if str(s).strip()})
    bad = [s for s in vals if s not in allowed]
    if bad:
        raise ToolError("invalid_sign", f"Unknown {label}: {bad}.", allowed=list(allowed))
    return vals


def _parse_observed_at(s):
    if not s:
        return db.now()
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        raise ToolError("invalid_time", f"observed_at '{s}' is not an ISO-8601 timestamp.")
    if t.tzinfo is None:
        t = t.replace(tzinfo=db.TZ)
    t = t.astimezone(db.TZ)
    n = db.now()
    if t > n + timedelta(minutes=5):
        raise ToolError("invalid_time", "observed_at is in the future.")
    if t < n - timedelta(hours=24):
        raise ToolError("invalid_time", "observed_at is more than 24 hours ago.")
    return t


def _patient(pid, age, sex, complaint):
    pid = (pid or "").strip()
    if not pid:
        raise ToolError("invalid_patient", "patient_id is required.")
    p = db.get_patient(pid)
    if p:
        return p
    if age is None or not sex or not complaint:
        raise ToolError(
            "unknown_patient",
            f"Patient '{pid}' is not registered. Provide age, sex and complaint to register them.",
        )
    sex = str(sex).strip().upper()[:1]
    if sex not in ("M", "F"):
        raise ToolError("invalid_patient", "sex must be M or F.")
    db.add_patient(pid, int(age), sex, str(complaint).strip())
    return db.get_patient(pid)


def _check_adult(p):
    if p["age"] < rules.ADULT_MIN_AGE:
        raise ToolError(
            "out_of_scope_paediatric",
            f"Patient {p['id']} is {p['age']}. This tool uses an adult scale (age {rules.ADULT_MIN_AGE}+). "
            "No score produced; a clinician must triage this patient directly.",
            needs_clinician=True,
        )


def _inputs(rr, hr, sbp, temp_c, spo2, avpu, mobility, trauma, emergency_signs, very_urgent_signs):
    em = _signs(emergency_signs, rules.EMERGENCY_SIGNS, "emergency_signs")
    vu = _signs(very_urgent_signs, rules.VERY_URGENT_SIGNS, "very_urgent_signs")
    v = rules.Vitals(
        rr=rr,
        hr=hr,
        sbp=sbp,
        temp_c=temp_c,
        spo2=spo2,
        avpu=str(avpu).strip().upper() if avpu else None,
        mobility=str(mobility).strip().lower() if mobility else None,
        trauma=bool(trauma),
    )
    errs = rules.validate(v)
    if errs:
        raise ToolError("invalid_vitals", " ".join(errs), errors=errs)
    miss = rules.missing(v)
    if miss and not em:  # emergency signs never wait for a full set of vitals
        raise ToolError(
            "missing_vitals",
            f"Missing: {miss}. Ask the nurse for these. Do not guess values.",
            missing=miss,
        )
    return v, em, vu


def _minutes(since_iso):
    return max(0, int((db.now() - datetime.fromisoformat(since_iso)).total_seconds() // 60))


# ---- tool 1: read-only ----------------------------------------------------------------
@audited
def get_queue(include_pending: bool = True):
    """Current waiting list with confirmed priorities, waiting time and pending items."""
    rows = []
    for p in db.list_patients():
        conf = db.confirmed(p["id"])
        last = db.last_observation(p["id"])
        pend = db.pending_proposals(p["id"])
        proposed = None
        for pr in reversed(pend):
            if pr["kind"] in ("priority", "recheck"):
                proposed = pr["payload"]["proposed_band"]
                break
        band = conf["final_band"] if conf else None
        wait = _minutes(p["arrived_at"])
        target = rules.TARGET_MINUTES.get(band) if band else None
        row = {
            "patient_id": p["id"],
            "age": p["age"],
            "sex": p["sex"],
            "complaint": p["complaint"],
            "minutes_waiting": wait,
            "confirmed_band": band,
            "confirmed_by": conf["decided_by"] if conf else None,
            "sent_to_bay_allowed": band is not None,
            "target_minutes": target,
            "overdue": (target is not None and wait > target),
            "needs_scoring": last is None and p["age"] >= rules.ADULT_MIN_AGE,
            "needs_clinician_direct": p["age"] < rules.ADULT_MIN_AGE,
            "last_tews": last["tews"] if last else None,
        }
        if include_pending:
            row["awaiting_clinician"] = [
                {"proposal_id": x["id"], "kind": x["kind"], **(
                    {"proposed_band": x["payload"]["proposed_band"]} if "proposed_band" in x["payload"]
                    else {"department": x["payload"].get("department")})}
                for x in pend
            ]
            row["proposed_band"] = proposed
        rows.append(row)

    def urgency(r):
        b = r["confirmed_band"] or r.get("proposed_band")
        return -(rules.band_index(b) if b else -1)

    rows.sort(key=lambda r: (urgency(r), -r["minutes_waiting"]))
    return {"ok": True, "as_of": db.now().isoformat(timespec="seconds"), "count": len(rows), "queue": rows}


# ---- tool 2: scores a priority (writes a PENDING proposal) ------------------------------
@audited
def score_priority(
    patient_id: str,
    rr=None, hr=None, sbp=None, temp_c=None, spo2=None,
    avpu=None, mobility=None, trauma=False,
    emergency_signs=None, very_urgent_signs=None,
    observed_at=None, age=None, sex=None, complaint=None,
):
    """Score measured vitals and PROPOSE a priority band. Needs clinician confirmation."""
    p = _patient(patient_id, age, sex, complaint)
    _check_adult(p)
    v, em, vu = _inputs(rr, hr, sbp, temp_c, spo2, avpu, mobility, trauma, emergency_signs, very_urgent_signs)
    ts = _parse_observed_at(observed_at)
    sc = rules.score(v, em, vu)
    obs_id = db.add_observation(p["id"], ts, v.as_dict(), sc)
    prop_id = db.add_proposal(
        p["id"],
        "priority",
        {"proposed_band": sc.band, "tews": sc.tews, "tews_band": sc.tews_band, "observation_id": obs_id},
        sc.evidence,
    )
    return {
        "ok": True,
        "patient_id": p["id"],
        "proposal_id": prop_id,
        "observation_id": obs_id,
        "tews": sc.tews,
        "proposed_band": sc.band,
        "evidence": sc.evidence,
        "status": "PENDING - not sent to a bay until a named clinician confirms this priority",
    }


# ---- tool 3: raises a re-check flag (writes a PENDING proposal) ---------------------------
@audited
def raise_recheck_flag(
    patient_id: str,
    rr=None, hr=None, sbp=None, temp_c=None, spo2=None,
    avpu=None, mobility=None, trauma=False,
    emergency_signs=None, very_urgent_signs=None,
    observed_at=None, reported_change: str = "",
):
    """Record fresh vitals and flag the patient for a clinician second look ONLY if they worsened."""
    p = db.get_patient((patient_id or "").strip())
    if not p:
        raise ToolError("unknown_patient", f"Patient '{patient_id}' is not registered.")
    _check_adult(p)
    prev = db.last_observation(p["id"])
    if not prev:
        raise ToolError("no_baseline", "No earlier observation to compare with. Use score_priority first.")
    v, em, vu = _inputs(rr, hr, sbp, temp_c, spo2, avpu, mobility, trauma, emergency_signs, very_urgent_signs)
    ts = _parse_observed_at(observed_at)
    sc = rules.score(v, em, vu)
    flag, reasons = rules.compare(prev, sc)
    obs_id = db.add_observation(p["id"], ts, v.as_dict(), sc)
    out = {
        "ok": True,
        "patient_id": p["id"],
        "observation_id": obs_id,
        "flag_raised": flag,
        "tews_before": prev["tews"],
        "tews_now": sc.tews,
        "evidence": reasons,
    }
    if flag:
        prop_id = db.add_proposal(
            p["id"],
            "recheck",
            {
                "proposed_band": sc.band,
                "tews": sc.tews,
                "from_observation": prev["id"],
                "to_observation": obs_id,
                "reported_change": (reported_change or "").strip(),
            },
            reasons + sc.evidence,
        )
        out.update(
            proposal_id=prop_id,
            proposed_band=sc.band,
            status="PENDING - clinician second look requested; the agent cannot act on this",
        )
    else:
        out["status"] = "No flag raised: no measured deterioration."
    return out


# ---- tool 4: routes a referral (writes a PENDING proposal) --------------------------------
@audited
def route_referral(patient_id: str, department: str, cited_fields=None):
    """Propose routing a patient to a department. Must cite recorded values; clinician approves."""
    dept = (department or "").strip().lower()
    if dept not in DEPARTMENTS:
        raise ToolError("invalid_department", f"Unknown department '{department}'.", allowed=list(DEPARTMENTS))
    p = db.get_patient((patient_id or "").strip())
    if not p:
        raise ToolError("unknown_patient", f"Patient '{patient_id}' is not registered.")
    last = db.last_observation(p["id"])
    if not last:
        raise ToolError("no_observation", "Score the patient first: a referral must cite recorded values.")
    fields = [str(f).strip().lower() for f in (cited_fields or []) if str(f).strip()]
    if not fields:
        raise ToolError("unsourced", "Cite at least one recorded field (e.g. 'spo2', 'chest_pain', 'complaint').")

    cited, bad = {}, []
    for f in fields:
        if f == "complaint":
            cited[f] = p["complaint"]
        elif f in last["vitals"] and last["vitals"][f] not in (None, False):
            cited[f] = last["vitals"][f]
        elif f in last["emergency_signs"] or f in last["very_urgent_signs"]:
            cited[f] = "reported"
        else:
            bad.append(f)
    if bad:
        raise ToolError(
            "unsourced",
            f"Not found in {p['id']}'s latest observation: {bad}. Cite only recorded values.",
            not_found=bad,
        )
    for pr in db.pending_proposals(p["id"]):  # idempotent: no duplicate pending referral
        if pr["kind"] == "referral" and pr["payload"].get("department") == dept:
            return {"ok": True, "duplicate": True, "proposal_id": pr["id"], "department": dept,
                    "status": "Already pending clinician approval"}
    evidence = [f"{k}: {v}" for k, v in cited.items()] + [f"From observation #{last['id']}"]
    prop_id = db.add_proposal(
        p["id"], "referral", {"department": dept, "cited": cited, "observation_id": last["id"]}, evidence
    )
    return {
        "ok": True,
        "patient_id": p["id"],
        "proposal_id": prop_id,
        "department": dept,
        "evidence": evidence,
        "status": "PENDING - referral not sent until a named clinician approves",
    }
