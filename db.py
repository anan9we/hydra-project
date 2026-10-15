"""SQLite storage. Local file only: no patient data leaves the machine.

Key idea (the human gate): the agent can only INSERT *pending* proposals.
`decide_proposal` (clinician approval) is NOT exposed to the agent / MCP server;
it is called from the CLI or Telegram bot after a named clinician acts.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import rules

try:  # Windows needs the `tzdata` package for this; fall back to fixed UTC+3 (Kenya, no DST)
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Africa/Nairobi")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=3))

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    age INTEGER NOT NULL,
    sex TEXT NOT NULL,
    complaint TEXT NOT NULL,
    arrived_at TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    vitals_json TEXT NOT NULL,
    emergency_json TEXT NOT NULL,
    very_urgent_json TEXT NOT NULL,
    components_json TEXT NOT NULL,
    tews INTEGER NOT NULL,
    tews_band TEXT NOT NULL,
    band TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL,
    kind TEXT NOT NULL,                -- priority | recheck | referral
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | superseded
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_note TEXT,
    final_band TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,               -- 'agent' or a clinician's name
    tool TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    ok INTEGER NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(TZ)


def db_path() -> str:
    return os.getenv("TRIAGE_DB", "triage.db")


@contextmanager
def conn():
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def reset():
    """Delete the database file (demo / tests)."""
    p = db_path()
    if os.path.exists(p):
        os.remove(p)
    init()


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


# ---- patients -------------------------------------------------------------------
def add_patient(pid, age, sex, complaint, arrived_at=None, synthetic=True):
    with conn() as c:
        c.execute(
            "INSERT INTO patients (id,age,sex,complaint,arrived_at,synthetic) VALUES (?,?,?,?,?,?)",
            (pid, int(age), sex, complaint, (arrived_at or now()).isoformat(), 1 if synthetic else 0),
        )


def get_patient(pid):
    with conn() as c:
        row = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def list_patients():
    with conn() as c:
        rows = c.execute("SELECT * FROM patients ORDER BY arrived_at").fetchall()
    return [dict(r) for r in rows]


# ---- observations ---------------------------------------------------------------
def add_observation(pid, observed_at, vitals: dict, sc: rules.Score) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO observations
            (patient_id,observed_at,vitals_json,emergency_json,very_urgent_json,components_json,
             tews,tews_band,band,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                pid,
                observed_at.isoformat(),
                _j(vitals),
                _j(sc.emergency_signs),
                _j(sc.very_urgent_signs),
                _j(sc.components),
                sc.tews,
                sc.tews_band,
                sc.band,
                _j(sc.evidence),
            ),
        )
        return cur.lastrowid


def _obs(row):
    if not row:
        return None
    d = dict(row)
    d["vitals"] = json.loads(d.pop("vitals_json"))
    d["emergency_signs"] = json.loads(d.pop("emergency_json"))
    d["very_urgent_signs"] = json.loads(d.pop("very_urgent_json"))
    d["components"] = json.loads(d.pop("components_json"))
    d["evidence"] = json.loads(d.pop("evidence_json"))
    return d


def last_observation(pid):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM observations WHERE patient_id=? ORDER BY id DESC LIMIT 1", (pid,)
        ).fetchone()
    return _obs(row)


def observations_for(pid):
    with conn() as c:
        rows = c.execute("SELECT * FROM observations WHERE patient_id=? ORDER BY id", (pid,)).fetchall()
    return [_obs(r) for r in rows]


# ---- proposals (agent writes pending; clinicians decide) -------------------------
def _prop(row):
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json"))
    d["evidence"] = json.loads(d.pop("evidence_json"))
    return d


def add_proposal(pid, kind, payload: dict, evidence: list) -> int:
    with conn() as c:
        if kind in ("priority", "recheck"):  # a newer proposal replaces a stale pending one
            c.execute(
                "UPDATE proposals SET status='superseded' WHERE patient_id=? AND status='pending' "
                "AND kind IN ('priority','recheck')",
                (pid,),
            )
        cur = c.execute(
            "INSERT INTO proposals (patient_id,kind,payload_json,evidence_json,created_at) VALUES (?,?,?,?,?)",
            (pid, kind, _j(payload), _j(evidence), now().isoformat()),
        )
        return cur.lastrowid


def get_proposal(prop_id):
    with conn() as c:
        row = c.execute("SELECT * FROM proposals WHERE id=?", (prop_id,)).fetchone()
    return _prop(row)


def pending_proposals(pid=None):
    with conn() as c:
        if pid:
            rows = c.execute(
                "SELECT * FROM proposals WHERE status='pending' AND patient_id=? ORDER BY id", (pid,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY id").fetchall()
    return [_prop(r) for r in rows]


def all_proposals():
    with conn() as c:
        rows = c.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    return [_prop(r) for r in rows]


def clinicians() -> list:
    """Named clinicians allowed to approve. Empty list = demo mode (any non-empty name)."""
    return [n.strip() for n in os.getenv("CLINICIANS", "").split(",") if n.strip()]


def decide_proposal(prop_id, clinician: str, approve: bool, note: str = "", final_band=None):
    """THE GATE. Called by a human-facing interface only (never exposed to the agent).

    For priority/recheck proposals the clinician may set `final_band` (override);
    an override requires a note. Returns the updated proposal.
    """
    clinician = (clinician or "").strip()
    if not clinician:
        raise PermissionError("A named clinician is required to decide a proposal.")
    allowed = clinicians()
    if allowed and clinician.lower() not in [a.lower() for a in allowed]:
        raise PermissionError(f"'{clinician}' is not on the clinician list.")
    prop = get_proposal(prop_id)
    if not prop:
        raise ValueError(f"No proposal #{prop_id}.")
    if prop["status"] != "pending":
        raise ValueError(f"Proposal #{prop_id} is already {prop['status']}.")

    fb = None
    if approve and prop["kind"] in ("priority", "recheck"):
        proposed = prop["payload"]["proposed_band"]
        fb = (final_band or proposed).upper()
        if fb not in rules.BANDS:
            raise ValueError(f"final_band must be one of {rules.BANDS}.")
        if fb != proposed and not note.strip():
            raise ValueError("Overriding the proposed band requires a note.")
    status = "approved" if approve else "rejected"
    with conn() as c:
        c.execute(
            "UPDATE proposals SET status=?, decided_at=?, decided_by=?, decision_note=?, final_band=? WHERE id=?",
            (status, now().isoformat(), clinician, note or "", fb, prop_id),
        )
    out = get_proposal(prop_id)
    log_audit(
        clinician,
        "decide_proposal",
        {"proposal_id": prop_id, "approve": approve, "note": note, "final_band": final_band},
        {"status": status, "final_band": fb, "kind": prop["kind"], "patient_id": prop["patient_id"]},
        True,
    )
    return out


def confirmed(pid):
    """The patient's current clinician-confirmed band, or None. Only humans create this."""
    with conn() as c:
        row = c.execute(
            "SELECT final_band, decided_by, decided_at, kind FROM proposals WHERE patient_id=? "
            "AND status='approved' AND kind IN ('priority','recheck') AND final_band IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
    return dict(row) if row else None


# ---- audit ----------------------------------------------------------------------
def log_audit(actor, tool, inputs, output, ok):
    with conn() as c:
        c.execute(
            "INSERT INTO audit_log (ts,actor,tool,input_json,output_json,ok) VALUES (?,?,?,?,?,?)",
            (now().isoformat(), actor, tool, _j(inputs), _j(output), 1 if ok else 0),
        )


def audit_rows(limit=None):
    with conn() as c:
        q = "SELECT * FROM audit_log ORDER BY id"
        rows = c.execute(q + (" DESC LIMIT ?" if limit else ""), (limit,) if limit else ()).fetchall()
    rows = [dict(r) for r in rows]
    return list(reversed(rows)) if limit else rows
