"""Shift statistics + de-identified digest (e-mailed) + audit CSV."""
import csv
import io
import json
import statistics
from datetime import datetime, timedelta

from . import db, gate


def stats(hours: int = 24) -> dict:
    since = (db.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    runs = db.rows("SELECT * FROM runs WHERE started_at>=?", (since,))
    done = [r for r in runs if r["ended_at"]]
    appr = db.rows("SELECT a.*, p.created_at proposed_at FROM approvals a JOIN proposals p ON p.id=a.proposal_id "
                   "WHERE a.decided_at>=?", (since,))
    lat = [(datetime.fromisoformat(a["decided_at"]) - datetime.fromisoformat(a["proposed_at"])).total_seconds() / 60 for a in appr]
    return {
        "runs": len(runs), "runs_failed": sum(1 for r in runs if r["outcome"] == "failed"),
        "avg_tool_calls": round(statistics.mean([r["tool_calls"] for r in done]), 1) if done else 0,
        "avg_tokens": round(statistics.mean([r["input_tokens"] + r["output_tokens"] for r in done])) if done else 0,
        "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in runs), 4),
        "pending": db.one("SELECT COUNT(*) n FROM proposals WHERE status='pending'")["n"],
        "approved": sum(1 for a in appr if a["decision"] == "approved"),
        "rejected": sum(1 for a in appr if a["decision"] == "rejected"),
        "overrides": sum(1 for a in appr if a["override_value"]),
        "median_minutes_to_decision": round(statistics.median(lat), 1) if lat else None,
    }


def overdue_rechecks() -> list[dict]:
    now = db.now().isoformat(timespec="seconds")
    return db.rows("SELECT patient_id, colour, recheck_due FROM queue WHERE recheck_due IS NOT NULL AND recheck_due < ?", (now,))


def build_digest(hours: int = 24) -> tuple[str, str, dict]:
    s = stats(hours)
    ok, chain = gate.verify_chain()
    colours = {}
    for q in gate.queue_view():
        colours[q["colour"]] = colours.get(q["colour"], 0) + 1
    body = "\n".join([
        f"Triage Desk shift digest (last {hours} h)", "",
        f"Agent runs: {s['runs']} (failed safely: {s['runs_failed']})",
        f"Average per run: {s['avg_tool_calls']} tool calls, {s['avg_tokens']} tokens; total cost ${s['total_cost_usd']}",
        f"Proposals: {s['pending']} pending, {s['approved']} approved, {s['rejected']} rejected, {s['overrides']} clinician overrides",
        f"Median time from proposal to clinician decision: {s['median_minutes_to_decision']} min",
        f"Live queue now: " + (", ".join(f"{k} {v}" for k, v in colours.items()) or "empty"),
        f"Re-checks overdue: {len(overdue_rechecks())}",
        f"Audit log: {chain}", "",
        "No patient names, vitals or complaint text are included in this e-mail.",
    ])
    return f"[Triage Desk] Shift digest - {s['approved']} approved, {s['pending']} pending", body, s


def audit_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["audit_id", "decided_at", "ticket", "action", "decision", "clinician", "override", "note", "row_hash"])
    for r in db.rows("SELECT * FROM approvals ORDER BY id"):
        snap = json.loads(r["snapshot"])
        w.writerow([r["id"], r["decided_at"], db.ticket(snap["patient_id"]), snap["action"], r["decision"],
                    r["clinician"], r["override_value"] or "", r["note"] or "", r["row_hash"]])
    return buf.getvalue()
