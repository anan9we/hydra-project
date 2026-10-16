"""Human approval gate. Lives in the HOST process and is NOT exposed over MCP: the agent has no path to it.
Only decide() can write the live queue, and every decision becomes a hash-chained audit row."""
import hashlib
import hmac
import json
import secrets

from . import db, sats

QUEUE_ORDER = ["Red", "Unscored", "Orange", "Yellow", "Green"]  # Unscored = needs vitals re-measured now
GENESIS = "0" * 64


class GateError(Exception):
    pass


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 120_000).hex()


def add_clinician(name: str, role: str, pin: str) -> None:
    salt = secrets.token_hex(16)
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO clinicians (name, role, pin_salt, pin_hash) VALUES (?,?,?,?)",
                  (name, role, salt, _hash_pin(pin, salt)))


def clinicians() -> list[dict]:
    return db.rows("SELECT name, role FROM clinicians ORDER BY name")


def _check_pin(name: str, pin: str) -> dict:
    c = db.one("SELECT * FROM clinicians WHERE name=?", (name,))
    if not c or not hmac.compare_digest(c["pin_hash"], _hash_pin(pin or "", c["pin_salt"])):
        raise GateError("Unknown clinician or wrong PIN.")  # same message for both: no user enumeration
    return c


def _row_hash(prev: str, r: dict) -> str:
    body = json.dumps([prev, r["proposal_id"], r["clinician"], r["decision"], r["override_value"],
                       r["note"], r["decided_at"], r["snapshot"]], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def decide(proposal_id: int, clinician: str, pin: str, decision: str, note: str = "",
           override_colour: str | None = None) -> dict:
    if decision not in ("approved", "rejected"):
        raise GateError("decision must be 'approved' or 'rejected'")
    who = _check_pin(clinician, pin)
    if override_colour:
        if override_colour not in sats.COLOURS:
            raise GateError(f"override must be one of {sats.COLOURS}")
        if not note.strip():
            raise GateError("An override needs a written reason in the note.")
    ts = db.now().isoformat(timespec="seconds")
    with db.conn() as c:
        c.execute("BEGIN IMMEDIATE")
        p = c.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not p:
            raise GateError(f"No proposal {proposal_id}.")
        if p["status"] != "pending":
            raise GateError(f"Proposal {proposal_id} is already {p['status']}.")
        payload, evidence = json.loads(p["payload"]), json.loads(p["evidence"])
        entries = [dict(r) for r in c.execute(
            "SELECT entry_id, kind, field, value, text, round FROM entries WHERE patient_id=? ORDER BY entry_id",
            (p["patient_id"],))]
        snapshot = json.dumps({"patient_id": p["patient_id"], "action": p["action"], "payload": payload,
                               "evidence": evidence, "rationale": p["rationale"], "proposed_at": p["created_at"],
                               "inputs": entries, "run_id": p["run_id"]}, sort_keys=True, ensure_ascii=False)
        last = c.execute("SELECT row_hash FROM approvals ORDER BY id DESC LIMIT 1").fetchone()
        prev = last["row_hash"] if last else GENESIS
        row = {"proposal_id": proposal_id, "clinician": f"{who['name']} ({who['role']})", "decision": decision,
               "override_value": override_colour, "note": note, "decided_at": ts, "snapshot": snapshot}
        c.execute("INSERT INTO approvals (proposal_id, clinician, decision, override_value, note, decided_at, "
                  "snapshot, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                  (proposal_id, row["clinician"], decision, override_colour, note, ts, snapshot, prev,
                   _row_hash(prev, row)))
        c.execute("UPDATE proposals SET status=? WHERE id=?", (decision, proposal_id))
        if decision == "approved":
            _apply(c, p, payload, who, override_colour, ts)
    return {"proposal_id": proposal_id, "decision": decision, "by": row["clinician"]}


def _ensure_row(c, pid, proposal_id, who, ts):
    if not c.execute("SELECT 1 FROM queue WHERE patient_id=?", (pid,)).fetchone():
        c.execute("INSERT INTO queue (patient_id, colour, tews, provisional, proposal_id, approved_by, updated_at) "
                  "VALUES (?,?,?,?,?,?,?)", (pid, "Unscored", None, 1, proposal_id, who["name"], ts))


def _apply(c, p, payload, who, override, ts):
    pid, from_ts = p["patient_id"], None
    if p["action"] == "set_priority":
        colour = override or payload["colour"]
        due = _plus_minutes(ts, sats.RECHECK_MINUTES[colour])
        c.execute("INSERT INTO queue (patient_id, colour, tews, provisional, proposal_id, approved_by, updated_at, "
                  "recheck_due) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(patient_id) DO UPDATE SET colour=excluded.colour,"
                  " tews=excluded.tews, provisional=excluded.provisional, proposal_id=excluded.proposal_id, "
                  "approved_by=excluded.approved_by, updated_at=excluded.updated_at, recheck_due=excluded.recheck_due",
                  (pid, colour, payload.get("tews"), 1 if payload.get("provisional") else 0, p["id"], who["name"], ts, due))
    elif p["action"] == "recheck_flag":
        _ensure_row(c, pid, p["id"], who, ts)
        c.execute("UPDATE queue SET recheck_due=?, updated_at=? WHERE patient_id=?",
                  (_plus_minutes(ts, payload.get("minutes", 60)), ts, pid))
    elif p["action"] == "referral":
        _ensure_row(c, pid, p["id"], who, ts)
        c.execute("UPDATE queue SET referral=?, updated_at=? WHERE patient_id=?",
                  (payload["department"], ts, pid))


def _plus_minutes(ts: str, minutes: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(ts) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def verify_chain() -> tuple[bool, str]:
    prev = GENESIS
    for r in db.rows("SELECT * FROM approvals ORDER BY id"):
        if r["prev_hash"] != prev or _row_hash(prev, r) != r["row_hash"]:
            return False, f"Audit chain broken at approval #{r['id']}"
        prev = r["row_hash"]
    return True, "Audit chain intact"


def queue_view() -> list[dict]:
    q = db.rows("SELECT q.*, p.display_name, p.age, p.sex FROM queue q JOIN patients p ON p.id=q.patient_id")
    return sorted(q, key=lambda r: (QUEUE_ORDER.index(r["colour"]), r["updated_at"]))


def pending_view() -> list[dict]:
    out = []
    for p in db.rows("SELECT * FROM proposals WHERE status='pending' ORDER BY id"):
        p["payload"], p["evidence"] = json.loads(p["payload"]), json.loads(p["evidence"])
        out.append(p)
    return out
