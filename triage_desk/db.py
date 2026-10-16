"""SQLite storage. One short-lived connection per operation (WAL mode so the MCP subprocess and the
web app can share the file)."""
import json
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

NAIROBI = ZoneInfo("Africa/Nairobi")


def now() -> datetime:
    return datetime.now(NAIROBI)


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.db_path(), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY, display_name TEXT, age INTEGER NOT NULL, sex TEXT,
    synthetic INTEGER NOT NULL DEFAULT 1, arrived_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (            -- what the nurse recorded: the only thing evidence may cite
    patient_id TEXT NOT NULL, entry_id TEXT NOT NULL, kind TEXT NOT NULL,   -- vital | symptom
    field TEXT, value TEXT, text TEXT, round INTEGER, recorded_at TEXT NOT NULL,
    PRIMARY KEY (patient_id, entry_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, provider TEXT, model TEXT,
    started_at TEXT NOT NULL, ended_at TEXT, tool_calls INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
    outcome TEXT, summary TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS tool_log (           -- readable log of every tool call the agent made
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT NOT NULL, tool TEXT NOT NULL,
    args TEXT, result TEXT, ok INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assertions (         -- discriminators the agent asserted AND the tool verified
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, patient_id TEXT NOT NULL,
    discriminator_id TEXT NOT NULL, entry_id TEXT NOT NULL, quote TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (          -- everything the agent wants to DO sits here, pending
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, patient_id TEXT NOT NULL,
    action TEXT NOT NULL,                       -- set_priority | recheck_flag | referral
    payload TEXT NOT NULL, evidence TEXT NOT NULL, rationale TEXT,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clinicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, role TEXT,
    pin_salt TEXT NOT NULL, pin_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (          -- hash-chained audit log
    id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER NOT NULL, clinician TEXT NOT NULL,
    decision TEXT NOT NULL, override_value TEXT, note TEXT, decided_at TEXT NOT NULL,
    snapshot TEXT NOT NULL, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue (              -- the LIVE queue: written ONLY by gate.decide(), never by the agent
    patient_id TEXT PRIMARY KEY, colour TEXT NOT NULL, tews INTEGER, provisional INTEGER DEFAULT 0,
    proposal_id INTEGER NOT NULL, approved_by TEXT NOT NULL, updated_at TEXT NOT NULL,
    recheck_due TEXT, referral TEXT
);
"""


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def add_patient(pid, name, age, sex, synthetic=True, symptoms=(), vitals_rounds=()):
    """symptoms: list of free-text notes. vitals_rounds: list of dicts field->value (round 1, 2, ...)."""
    from .sats import FIELDS

    ts = now().isoformat(timespec="seconds")
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO patients VALUES (?,?,?,?,?,?)",
                  (pid, name, age, sex, 1 if synthetic else 0, ts))
        c.execute("DELETE FROM entries WHERE patient_id=?", (pid,))
        for i, text in enumerate(symptoms, 1):
            c.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)",
                      (pid, f"s{i}", "symptom", "note", None, text, None, ts))
        for rnd, vit in enumerate(vitals_rounds, 1):
            for field, value in vit.items():
                c.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)",
                          (pid, f"v{rnd}.{FIELDS[field][0]}", "vital", field, str(value), None, rnd, ts))


def rows(sql, params=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql, params=()):
    r = rows(sql, params)
    return r[0] if r else None


def execute(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).lastrowid


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


def meta_get_or_create(key: str, factory) -> str:
    with conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if r:
            return r["value"]
        v = factory()
        c.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, v))
        return c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()["value"]


def ticket(patient_id: str) -> str:
    """Pseudonymous id used in e-mails: random per-install salt, so it cannot be brute-forced from 'P-001'."""
    import hashlib
    import secrets
    salt = meta_get_or_create("ticket_salt", lambda: secrets.token_hex(16))
    return "T-" + hashlib.sha256(f"{salt}:{patient_id}".encode()).hexdigest()[:6]


def next_patient_id() -> str:
    nums = [int(m.group(1)) for r in rows("SELECT id FROM patients") if (m := re.fullmatch(r"P-(\d+)", r["id"]))]
    return f"P-{(max(nums) if nums else 0) + 1:03d}"


def add_vitals_round(pid: str, vitals: dict) -> int:
    """Append a new round of vitals (a re-check). Returns the round number."""
    from .sats import FIELDS
    ts = now().isoformat(timespec="seconds")
    with conn() as c:
        last = c.execute("SELECT COALESCE(MAX(round),0) m FROM entries WHERE patient_id=? AND kind='vital'", (pid,)).fetchone()["m"]
        rnd = last + 1
        for field, value in vitals.items():
            c.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)",
                      (pid, f"v{rnd}.{FIELDS[field][0]}", "vital", field, str(value), None, rnd, ts))
    return rnd
