# Triage Desk (work in progress)

An MCP-powered agent for the hospital triage desk. It **scores and presents** the queue.
It **never diagnoses, never prescribes, and never decides who is seen**: a named
nurse or clinician confirms every priority before a patient is sent to a bay.

> All data in this repo is **synthetic**. Not for clinical use. Scoring thresholds are a
> simplified SATS-style demonstration and must be verified before any real-world use.

## Build status
- [x] Bit 1: scoring rules, SQLite store with the clinician gate, MCP server (4 tools), tests
- [ ] Bit 2: agent (open-source framework + open-weights model) and CLI demo
- [ ] Bit 3: Telegram bot with clinician approve/override buttons
- [ ] Bit 4: Gmail OAuth report email
- [ ] Bit 5: ARCHITECTURE.md, EVALS.md, one-command run

## Try bit 1 now
```bash
python -m venv venv
venv\Scripts\activate          # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
python -m unittest discover -s tests -v     # 29 tests, no extra packages needed
python -m triage.seed --reset               # load synthetic patients
python -m triage.mcp_server                 # starts the MCP server on stdio (Ctrl+C to stop)
```

## MCP tools (our own server)
| Tool | Type | What it does |
|------|------|--------------|
| `get_queue` | read-only | Waiting list with confirmed bands, wait times, overdue flags |
| `score_priority` | acts | Scores vitals, writes a **pending** priority proposal with cited evidence |
| `raise_recheck_flag` | acts | Compares new vitals with the last set; flags for a clinician only on measured worsening |
| `route_referral` | acts | Writes a **pending** referral that must cite recorded values |

There is no tool that confirms anything. Confirmation lives in `triage/db.py::decide_proposal`
and is reachable only from human-facing interfaces.

## Licence
MIT
