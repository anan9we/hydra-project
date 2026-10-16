"""The six tools the agent can call. Plain functions (easy to test); mcp_server.py exposes them over MCP.

Design rules enforced HERE, outside the model:
  * the agent never supplies a score or a colour; both are derived from cited entries
  * every claim must quote the nurse's own entry; the quote is verified by substring match
  * nothing here can change the live queue; actions are written as PENDING proposals
"""
import functools
import os
import time

from mcp.server.mcpserver.exceptions import ToolError

from . import config, db, sats


class TriageToolError(ToolError):
    """Readable error returned to the agent (is_error=True, message visible) so it can correct itself."""


def _run_id():
    v = os.getenv("TRIAGE_RUN_ID")
    return int(v) if v else None


def logged(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ok, out = 1, None
        try:
            out = fn(*args, **kwargs)
            return out
        except TriageToolError as e:
            ok, out = 0, {"error": str(e)}
            raise
        finally:
            db.execute(
                "INSERT INTO tool_log (run_id, ts, tool, args, result, ok) VALUES (?,?,?,?,?,?)",
                (_run_id(), db.now().isoformat(timespec="seconds"), fn.__name__,
                 db.dumps(kwargs or args), db.dumps(out)[:4000], ok),
            )
    return wrapper


def _patient(pid):
    p = db.one("SELECT * FROM patients WHERE id=?", (pid,))
    if not p:
        raise TriageToolError(f"Unknown patient_id '{pid}'.")
    if config.is_cloud_llm() and not p["synthetic"]:
        raise TriageToolError("RESIDENCY_BLOCKED: this patient is not synthetic and the model endpoint is "
                              "not local. Real patient data must not leave the country.")
    return p


def _entries(pid):
    return db.rows("SELECT * FROM entries WHERE patient_id=? ORDER BY kind DESC, round, entry_id", (pid,))


def _vitals_by_round(entries):
    rounds = {}
    for e in entries:
        if e["kind"] == "vital":
            rounds.setdefault(e["round"], {})[e["field"]] = e["value"]
    return rounds


def _latest_tews(pid):
    rounds = _vitals_by_round(_entries(pid))
    if not rounds:
        return None, None, sats.compute_tews({}), None
    last = max(rounds)
    cur = sats.compute_tews(rounds[last])
    prev = sats.compute_tews(rounds[last - 1]) if last - 1 in rounds else None
    return last, rounds[last], cur, prev


@logged
def get_intake(patient_id: str) -> dict:
    p = _patient(patient_id)
    ents = _entries(patient_id)
    out_entries, text_all = [], ""
    for e in ents:
        if e["kind"] == "symptom":
            out_entries.append({"entry_id": e["entry_id"], "kind": "symptom", "text": e["text"]})
            text_all += " " + (e["text"] or "")
        else:
            out_entries.append({"entry_id": e["entry_id"], "kind": "vital", "field": e["field"],
                                "value": e["value"], "round": e["round"]})
    return {  # no name: data minimisation, the model never needs it
        "patient_id": p["id"], "age": p["age"], "sex": p["sex"], "arrived_at": p["arrived_at"],
        "adult_scale_applies": p["age"] >= sats.ADULT_MIN_AGE,
        "entries": out_entries,
        "candidate_discriminators_hint": sats.keyword_candidates(text_all),
        "note": "Hints are keyword matches only. To use one you must call assert_discriminator with a quote.",
    }


@logged
def score_tews(patient_id: str) -> dict:
    p = _patient(patient_id)
    if p["age"] < sats.ADULT_MIN_AGE:
        return {"supported": False, "reason": f"Age {p['age']}: paediatric SATS applies; this tool only "
                "scores adults. Do not guess; refer to a human clinician / paediatrics."}
    rnd, _, cur, prev = _latest_tews(patient_id)
    if rnd is None:
        return {"supported": True, "complete": False, "missing": list(sats.FIELDS),
                "message": "No vitals recorded. Propose a re-check flag so vitals get measured."}
    comps = {f: {**c, "entry_id": f"v{rnd}.{sats.FIELDS[f][0]}"} for f, c in cur["components"].items()}
    out = {"supported": True, "round": rnd, "complete": cur["complete"], "components": comps,
           "missing": cur["missing"], "invalid": cur["invalid"], "partial_total": cur["partial_total"],
           "tews_total": cur["total"], "tews_colour": cur["colour"]}
    if prev and prev["complete"] and cur["complete"]:
        out["previous_tews"] = prev["total"]
        out["delta"] = cur["total"] - prev["total"]
    return out


@logged
def assert_discriminator(patient_id: str, discriminator_id: str, entry_id: str, quote: str) -> dict:
    _patient(patient_id)
    if discriminator_id not in sats.DISCRIMINATORS:
        raise TriageToolError(f"Unknown discriminator_id '{discriminator_id}'. Valid ids: "
                              f"{', '.join(sats.DISCRIMINATORS)}.")
    e = db.one("SELECT * FROM entries WHERE patient_id=? AND entry_id=?", (patient_id, entry_id))
    if not e or e["kind"] != "symptom":
        raise TriageToolError(f"entry_id '{entry_id}' is not a symptom entry of {patient_id}. "
                              "Cite a symptom entry id such as 's1'.")
    q = (quote or "").strip()
    if len(q) < 3 or q.lower() not in (e["text"] or "").lower():
        raise TriageToolError(f"Quote not found verbatim in entry {entry_id}. Copy the exact words from "
                              f"the nurse's note. Entry text: {e['text']!r}")
    db.execute("INSERT INTO assertions (run_id, patient_id, discriminator_id, entry_id, quote, ts) "
               "VALUES (?,?,?,?,?,?)",
               (_run_id(), patient_id, discriminator_id, entry_id, q, db.now().isoformat(timespec="seconds")))
    return {"accepted": True, "discriminator": discriminator_id,
            "tier": sats.DISCRIMINATORS[discriminator_id][0], "colour_floor": sats.discriminator_colour(discriminator_id)}


def _run_assertions(pid):
    rid = _run_id()
    return db.rows("SELECT * FROM assertions WHERE patient_id=? AND run_id IS ?", (pid, rid))


@logged
def submit_priority(patient_id: str, rationale: str) -> dict:
    """Creates a PENDING priority proposal. Colour = max(TEWS colour, verified discriminators)."""
    p = _patient(patient_id)
    try:
        sats.guard_text(rationale)
    except ValueError as e:
        raise TriageToolError(str(e))
    if p["age"] < sats.ADULT_MIN_AGE:
        raise TriageToolError("Adult scale does not apply (age < 12). Do not propose a priority; "
                              "call route_referral to paediatrics instead.")
    if db.one("SELECT id FROM proposals WHERE patient_id=? AND action='set_priority' AND run_id IS ?",
              (patient_id, _run_id())):
        raise TriageToolError("A priority proposal for this patient already exists in this run.")
    rnd, _, cur, _prev = _latest_tews(patient_id)
    asserts = _run_assertions(patient_id)
    dcols = [sats.discriminator_colour(a["discriminator_id"]) for a in asserts]
    if not cur["complete"] and not dcols:
        raise TriageToolError(f"Cannot propose a priority: vitals incomplete (missing: {cur['missing']}) and no "
                              "verified discriminator. Call flag_recheck so the missing vitals are measured.")
    candidates = ([cur["colour"]] if cur["complete"] else []) + dcols
    colour = max(candidates, key=lambda c: sats.RANK[c])
    evidence = []
    for f, c in cur["components"].items():
        if c["points"] > 0 or cur["complete"]:
            evidence.append({"entry_id": f"v{rnd}.{sats.FIELDS[f][0]}", "kind": "vital", "field": f,
                             "value": c["value"], "points": c["points"]})
    for a in asserts:
        evidence.append({"entry_id": a["entry_id"], "kind": "symptom", "discriminator": a["discriminator_id"],
                         "quote": a["quote"]})
    if not evidence:
        raise TriageToolError("No evidence to cite; refusing to propose an unsourced priority.")
    payload = {"colour": colour, "tews": cur["total"], "tews_colour": cur["colour"],
               "provisional": not cur["complete"], "missing_vitals": cur["missing"],
               "recheck_minutes": sats.RECHECK_MINUTES[colour]}
    pid_ = db.execute(
        "INSERT INTO proposals (run_id, patient_id, action, payload, evidence, rationale, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_run_id(), patient_id, "set_priority", db.dumps(payload), db.dumps(evidence), rationale, "pending",
         db.now().isoformat(timespec="seconds")))
    return {"proposal_id": pid_, "status": "pending_clinician_approval", **payload, "evidence_count": len(evidence)}


@logged
def flag_recheck(patient_id: str, entry_ids: list[str], reason: str, minutes: int | None = None) -> dict:
    p = _patient(patient_id)
    try:
        sats.guard_text(reason)
    except ValueError as e:
        raise TriageToolError(str(e))
    have = {e["entry_id"] for e in _entries(patient_id)}
    bad = [i for i in (entry_ids or []) if i not in have]
    if not entry_ids and (_latest_tews(patient_id)[0] is not None):
        raise TriageToolError("Cite at least one entry_id that justifies the re-check.")
    if bad:
        raise TriageToolError(f"Unknown entry ids {bad}. Existing ids: {sorted(have)}")
    rnd, _, cur, prev = _latest_tews(patient_id)
    if minutes is None:
        minutes = sats.RECHECK_MINUTES.get(cur["colour"] or "Yellow", 60) or 10
    evidence = [{"entry_id": i, "kind": "vital" if i.startswith("v") else "symptom"} for i in (entry_ids or [])]
    if not evidence:
        evidence = [{"entry_id": "none", "kind": "missing_data",
                     "note": f"no vitals recorded; missing {cur['missing'] or list(sats.FIELDS)}"}]
    payload = {"minutes": int(minutes), "missing_vitals": cur["missing"],
               "delta": (cur["total"] - prev["total"]) if (prev and prev["complete"] and cur["complete"]) else None}
    prid = db.execute(
        "INSERT INTO proposals (run_id, patient_id, action, payload, evidence, rationale, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_run_id(), patient_id, "recheck_flag", db.dumps(payload), db.dumps(evidence), reason, "pending",
         db.now().isoformat(timespec="seconds")))
    return {"proposal_id": prid, "status": "pending_clinician_approval", **payload}


@logged
def route_referral(patient_id: str, department_id: str, entry_id: str, quote: str, reason: str) -> dict:
    _patient(patient_id)
    if department_id not in sats.DEPARTMENTS:
        raise TriageToolError(f"Unknown department_id '{department_id}'. Valid: {', '.join(sats.DEPARTMENTS)}")
    try:
        sats.guard_text(reason)
    except ValueError as e:
        raise TriageToolError(str(e))
    e = db.one("SELECT * FROM entries WHERE patient_id=? AND entry_id=?", (patient_id, entry_id))
    p = db.one("SELECT age FROM patients WHERE id=?", (patient_id,))
    if not e:
        raise TriageToolError(f"Unknown entry_id '{entry_id}'.")
    if e["kind"] == "symptom" and (quote or "").strip().lower() not in (e["text"] or "").lower():
        raise TriageToolError(f"Quote not found verbatim in entry {entry_id}. Entry text: {e['text']!r}")
    if e["kind"] == "vital":
        quote = f"{e['field']}={e['value']}"
    evidence = [{"entry_id": entry_id, "kind": e["kind"], "quote": quote}]
    prid = db.execute(
        "INSERT INTO proposals (run_id, patient_id, action, payload, evidence, rationale, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_run_id(), patient_id, "referral",
         db.dumps({"department_id": department_id, "department": sats.DEPARTMENTS[department_id],
                   "age": p["age"]}),
         db.dumps(evidence), reason, "pending", db.now().isoformat(timespec="seconds")))
    return {"proposal_id": prid, "status": "pending_clinician_approval", "department": sats.DEPARTMENTS[department_id]}


TOOLS = [get_intake, score_tews, assert_discriminator, submit_priority, flag_recheck, route_referral]
