"""Runs the agent for one patient, records the run (tokens, cost, time, outcome), fails safe."""
import asyncio
import os
import time

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from . import config, db
from .agent import RunDeps, build_agent, build_toolset
from .llm import build_model


class ResidencyError(Exception):
    pass


async def run_triage_async(patient_id: str, model=None, on_call=None) -> dict:
    db.init()
    p = db.one("SELECT * FROM patients WHERE id=?", (patient_id,))
    if not p:
        raise ValueError(f"Unknown patient {patient_id}")
    if config.is_cloud_llm() and not p["synthetic"]:
        raise ResidencyError("Blocked: this patient is not synthetic and the model endpoint is not local.")
    run_id = db.execute("INSERT INTO runs (patient_id, provider, model, started_at) VALUES (?,?,?,?)",
                        (patient_id, config.provider(), config.model_name(), db.now().isoformat(timespec="seconds")))
    t0, outcome, summary, err, usage = time.time(), "failed", None, None, None
    try:
        toolset = build_toolset(run_id, on_call)
        agent = build_agent(model or build_model(), toolset)
        async with agent:
            res = await agent.run(f"Triage patient {patient_id}.", deps=RunDeps(run_id=run_id, patient_id=patient_id),
                                  model_settings={"temperature": 0})
        usage = res.usage() if callable(res.usage) else res.usage  # v2: property
        outcome, summary = res.output.outcome, res.output.summary
        from .emailer import alert_pending  # never raises; e-mail trouble must not block triage
        mail = alert_pending(res.output.proposal_ids)
        return {"run_id": run_id, "ok": True, "outcome": outcome, "summary": summary,
                "proposal_ids": res.output.proposal_ids, "missing": res.output.missing, "email": mail}
    except Exception as e:  # fail safe: no proposal is invented; patient stays in arrival order
        err = f"{type(e).__name__}: {e}"
        return {"run_id": run_id, "ok": False, "outcome": "failed", "error": err, "proposal_ids": []}
    finally:
        pin, pout = config.price_per_million_tokens()
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        calls = db.one("SELECT COUNT(*) n FROM tool_log WHERE run_id=?", (run_id,))["n"]
        db.execute("UPDATE runs SET ended_at=?, tool_calls=?, input_tokens=?, output_tokens=?, cost_usd=?, "
                   "outcome=?, summary=?, error=? WHERE id=?",
                   (db.now().isoformat(timespec="seconds"), calls, it, ot, it / 1e6 * pin + ot / 1e6 * pout,
                    outcome, summary, err, run_id))


def run_triage(patient_id: str, model=None, on_call=None) -> dict:
    return asyncio.run(run_triage_async(patient_id, model, on_call))
