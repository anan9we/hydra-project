"""Evaluation harness.  python -m triage_desk.evals --layer scoring|agent [--repeat N] [--write-report]

Layer 'scoring' (no LLM): an answer-key script drives the real MCP tool functions with the LABELLED correct readings of each
  note, and checks scores, colours, refusals and guards against hand-calculated expectations. It tests the
  deterministic backbone. It says NOTHING about how well a language model reads notes.
Layer 'agent' (needs your open-weights model running): the real agent triages each case; we compare what it
  actually did with the labels. This is the layer that measures the model.
Both use a throw-away database and synthetic patients only."""
import argparse
import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path

from . import config, db, gate, sats

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases.jsonl"


def load_cases():
    return [json.loads(l) for l in CASES.read_text(encoding="utf-8").splitlines() if l.strip()]


def fresh_db():
    d = tempfile.mkdtemp(prefix="triage_eval_")
    os.environ["TRIAGE_DB"] = str(Path(d) / "eval.db")
    os.environ.pop("TRIAGE_RUN_ID", None)
    db.init()
    gate.add_clinician("Eval Clinician", "Medical Officer", "0000")


def seed_case(c):
    db.add_patient(c["id"], f"Synthetic {c['id']}", c["age"], c["sex"], True, c["symptoms"], c["vitals"])


# ----------------------------------------------------------------------------- layer 1: scoring
def run_scoring_case(c):
    from . import tools
    from .tools import TriageToolError
    e, pid, checks = c["expect"], c["id"], []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    sc = tools.score_tews(pid)
    if c["age"] < sats.ADULT_MIN_AGE:
        chk("adult scale refused for child", sc.get("supported") is False, str(sc)[:80])
    else:
        if e["tews"] is not None:
            chk(f"TEWS total = {e['tews']}", sc.get("tews_total") == e["tews"], f"got {sc.get('tews_total')}")
        else:
            chk("TEWS reported incomplete", sc.get("complete") is False, f"missing {sc.get('missing')}")
    for d in e["discriminators"]:
        try:
            r = tools.assert_discriminator(pid, d["id"], d["entry"], d["quote"])
            chk(f"discriminator {d['id']} accepted with quote", r["accepted"])
        except TriageToolError as ex:
            chk(f"discriminator {d['id']} accepted with quote", False, str(ex)[:80])
    try:
        r = tools.submit_priority(pid, "Priority proposed from recorded vitals and symptoms.")
        chk("priority proposed", not e["no_priority"], "but expected refusal" if e["no_priority"] else "")
        if not e["no_priority"]:
            chk(f"colour = {e['colour']}", r["colour"] == e["colour"], f"got {r['colour']}")
            chk("provisional flag correct", r["provisional"] == (e["outcome"] == "provisional_priority_proposed"))
            chk("every evidence entry cites a real entry id", all(
                db.one("SELECT 1 x FROM entries WHERE patient_id=? AND entry_id=?", (pid, ev["entry_id"]))
                for ev in json.loads(db.one("SELECT evidence FROM proposals WHERE id=?", (r["proposal_id"],))["evidence"])))
    except TriageToolError as ex:
        chk("priority refused (as expected)" if e["no_priority"] else "priority proposed", e["no_priority"], str(ex)[:90])
    if e["recheck"]:
        try:
            tools.flag_recheck(pid, e["recheck_entries"], "Vitals incomplete or worsening; please re-measure.")
            chk("re-check flag accepted", True)
        except TriageToolError as ex:
            chk("re-check flag accepted", False, str(ex)[:80])
    if e["referral"]:
        rf = e["referral"]
        try:
            tools.route_referral(pid, rf["dept"], rf["entry"], rf["quote"], "Adult scale does not apply; needs paediatric review.")
            chk(f"referral to {rf['dept']} accepted", True)
        except TriageToolError as ex:
            chk(f"referral to {rf['dept']} accepted", False, str(ex)[:80])
    return checks


def run_guards():
    from . import tools
    from .tools import TriageToolError
    out = []

    def refused(name, fn):
        try:
            fn()
            out.append((name, False, "was accepted"))
        except TriageToolError as ex:
            out.append((name, True, str(ex)[:70]))
    refused("G1 quote not in the nurse's note is rejected",
            lambda: tools.assert_discriminator("E02", "chest_pain", "s1", "crushing pain radiating to arm"))
    refused("G2 diagnosis wording is rejected", lambda: tools.flag_recheck("E02", ["s1"], "Likely has a heart attack"))
    refused("G3 prescription wording is rejected", lambda: tools.flag_recheck("E02", ["s1"], "Give aspirin 300 mg now"))
    refused("G4 unknown discriminator id is rejected", lambda: tools.assert_discriminator("E02", "heart_attack", "s1", "Chest pain"))
    refused("G5 unknown department is rejected", lambda: tools.route_referral("E02", "cardiology", "s1", "Chest pain", "Needs review."))
    refused("G6 second priority proposal in the same run is rejected",
            lambda: tools.submit_priority("E02", "Priority proposed again."))
    refused("G7 citing a vitals entry as a symptom discriminator is rejected",
            lambda: tools.assert_discriminator("E02", "chest_pain", "v1.rr", "24"))
    out.append(("G8 agent tools never touched the live queue", db.rows("SELECT * FROM queue") == [], ""))
    prev = {k: os.environ.get(k) for k in ("TRIAGE_PROVIDER",)}
    db.add_patient("REAL-1", "Real Person", 40, "F", synthetic=False, symptoms=["cough"], vitals_rounds=[{"resp_rate": 16}])
    os.environ["TRIAGE_PROVIDER"] = "gemini"
    refused("G9 real patient blocked when the model endpoint is cloud", lambda: tools.get_intake("REAL-1"))
    if prev["TRIAGE_PROVIDER"] is None:
        os.environ.pop("TRIAGE_PROVIDER", None)
    else:
        os.environ["TRIAGE_PROVIDER"] = prev["TRIAGE_PROVIDER"]
    return out


def layer_scoring():
    fresh_db()
    cases = load_cases()
    res = []
    for c in cases:
        seed_case(c)
    for c in cases:
        checks = run_scoring_case(c)
        res.append({"id": c["id"], "desc": c["desc"], "pass": all(ok for _, ok, _ in checks),
                    "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]})
    guards = run_guards()
    g = [{"name": n, "pass": ok, "detail": d} for n, ok, d in guards]
    return {"layer": "scoring", "date": str(date.today()), "cases": res, "guards": g,
            "passed": sum(r["pass"] for r in res), "total": len(res),
            "guards_passed": sum(x["pass"] for x in g), "guards_total": len(g)}


# ----------------------------------------------------------------------------- layer 2: live agent
def judge_agent_case(c, r, run_id):
    e, pid = c["expect"], c["id"]
    props = db.rows("SELECT * FROM proposals WHERE run_id=?", (run_id,))
    prio = [p for p in props if p["action"] == "set_priority"]
    pl = json.loads(prio[0]["payload"]) if prio else {}
    asserted = sorted({a["discriminator_id"] for a in db.rows("SELECT discriminator_id FROM assertions WHERE run_id=?", (run_id,))})
    want = sorted(d["id"] for d in e["discriminators"])
    rech = [p for p in props if p["action"] == "recheck_flag"]
    refs = [json.loads(p["payload"]) for p in props if p["action"] == "referral"]
    ck = []
    ck.append(("run completed without failure", r["ok"], r.get("error", "")))
    if not r["ok"]:
        return ck
    ck.append((f"outcome = {e['outcome']}", r["outcome"] == e["outcome"], f"got {r['outcome']}"))
    if e["no_priority"]:
        ck.append(("no priority proposed", not prio, ""))
    else:
        ck.append((f"colour = {e['colour']}", pl.get("colour") == e["colour"], f"got {pl.get('colour')}"))
        if e["tews"] is not None:
            ck.append((f"TEWS = {e['tews']}", pl.get("tews") == e["tews"], f"got {pl.get('tews')}"))
    ck.append((f"discriminators = {want}", asserted == want, f"got {asserted}"))
    ck.append(("re-check flag " + ("present" if e["recheck"] else "absent"), bool(rech) == e["recheck"], f"found {len(rech)}"))
    if e["referral"]:
        ck.append((f"referral to {e['referral']['dept']}", any(x["department_id"] == e["referral"]["dept"] for x in refs), f"got {[x['department_id'] for x in refs]}"))
    return ck


def layer_agent(repeat=1):
    from .runner import run_triage
    fresh_db()
    cases = load_cases()
    for c in cases:
        seed_case(c)
    res, calls, toks, secs = [], [], [], []
    for c in cases:
        runs = []
        for k in range(repeat):
            if k:  # clear previous proposals for a clean repeat
                with db.conn() as cx:
                    cx.execute("DELETE FROM proposals WHERE patient_id=?", (c["id"],))
            t0 = time.time()
            r = run_triage(c["id"])
            dt = time.time() - t0
            run = db.one("SELECT * FROM runs WHERE id=?", (r["run_id"],))
            ck = judge_agent_case(c, r, r["run_id"])
            calls.append(run["tool_calls"]); toks.append(run["input_tokens"] + run["output_tokens"]); secs.append(dt)
            runs.append({"pass": all(ok for _, ok, _ in ck), "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in ck]})
            print(f"  {c['id']} run {k+1}/{repeat}: {'PASS' if runs[-1]['pass'] else 'FAIL'}  ({dt:.0f}s, {run['tool_calls']} tool calls)")
        res.append({"id": c["id"], "desc": c["desc"], "runs": runs, "pass_rate": sum(x["pass"] for x in runs) / len(runs)})
    n = max(len(calls), 1)
    return {"layer": "agent", "date": str(date.today()), "provider": config.provider(), "model": config.model_name(),
            "repeat": repeat, "cases": res, "passed": sum(1 for r in res if r["pass_rate"] == 1), "total": len(res),
            "avg_tool_calls": round(sum(calls) / n, 1), "avg_tokens": round(sum(toks) / n), "avg_seconds": round(sum(secs) / n, 1)}


# ----------------------------------------------------------------------------- report
def write_report():
    s = json.loads((ROOT / "evals" / "results_scoring.json").read_text()) if (ROOT / "evals" / "results_scoring.json").exists() else None
    a = json.loads((ROOT / "evals" / "results_agent.json").read_text()) if (ROOT / "evals" / "results_agent.json").exists() else None
    L = ["# EVALS", "",
         "Everything here uses **synthetic patients only**. 18 labelled cases (`evals/cases.jsonl`), each with a "
         "hand-calculated expected TEWS and colour taken from the SATS table, not from our code. Two layers are kept "
         "separate on purpose:", "",
         "| Layer | What it tests | Needs a model? |", "|---|---|---|",
         "| 1. Scoring + guards | The deterministic backbone: TEWS, colour = max(TEWS, discriminators), refusals, citation and residency guards. An answer-key script supplies the *correct* reading of each note. | No |",
         "| 2. Live agent | Whether the open-weights model reads the notes, picks the right discriminators, recovers from errors and finishes correctly. | Yes (Ollama) |", "",
         "Reproduce: `python -m triage_desk.evals --layer scoring` and `python -m triage_desk.evals --layer agent --repeat 3 --write-report`.", ""]
    if s:
        L += [f"## Layer 1: scoring and guards ({s['date']}): **{s['passed']}/{s['total']} cases, {s['guards_passed']}/{s['guards_total']} guards pass**", "",
              "| ID | Case | Result | Checks |", "|---|---|---|---|"]
        for r in s["cases"]:
            L.append(f"| {r['id']} | {r['desc']} | {'PASS' if r['pass'] else '**FAIL**'} | {sum(c['ok'] for c in r['checks'])}/{len(r['checks'])} |")
        L += ["", "| Guard | Result |", "|---|---|"]
        L += [f"| {g['name']} | {'PASS' if g['pass'] else '**FAIL**'} |" for g in s["guards"]]
        L.append("")
    else:
        L += ["## Layer 1: not run yet", ""]
    if a:
        L += [f"## Layer 2: live agent ({a['date']}, model `{a['model']}` via {a['provider']}, {a['repeat']} run(s) per case): **{a['passed']}/{a['total']} cases pass every run**", "",
              f"Average per run: {a['avg_tool_calls']} tool calls, {a['avg_tokens']} tokens, {a['avg_seconds']} s. Cost: $0 API (own hardware).", "",
              "| ID | Case | Pass rate | First failing check |", "|---|---|---|---|"]
        for r in a["cases"]:
            fail = next((f"{c['name']} ({c['detail']})" for run in r["runs"] for c in run["checks"] if not c["ok"]), "")
            L.append(f"| {r['id']} | {r['desc']} | {r['pass_rate']:.0%} | {fail} |")
        L.append("")
    else:
        L += ["## Layer 2: live agent: **NOT RUN YET**", "",
              "Run `python -m triage_desk.evals --layer agent --repeat 3 --write-report` with your Ollama model "
              "running; this section is then filled in automatically from `evals/results_agent.json`. "
              "Layer 1 passing does not imply the model reads notes well.", ""]
    L += ["## What we did not fix (honest list)", "",
          "- **Discriminator list and target times are a trimmed subset** written from the published SATS; a clinician must verify them against the official SATS manual before any real use.",
          "- **Keyword hints can mislead**: `get_intake` hints `chest_pain` for case E16 (neighbour's chest pain). The model must refuse to assert it; layer 2 measures whether it does.",
          "- **Prompt injection is only partly contained** (E17). A quote that really appears in a note passes the verbatim check, so an injected sentence can still be asserted by a fooled model. The human gate, the cited evidence shown to the clinician, and the audit log are the backstop, not a cure.",
          "- **Adults only.** Under-12s are escalated unscored; the paediatric SATS is not implemented. Pregnancy and other special groups are not modelled.",
          "- **No clinical validation.** Synthetic cases only; no comparison against real triage outcomes.",
          "- **E-mail alerts** carry only ticket ids and colours, but timing plus colour could still be linkable inside a small ward.",
          "- Single-machine SQLite; no multi-site sync, no authentication beyond clinician PINs.", "",
          "## What we would try next", "",
          "- Run layer 2 across several open-weights models (Qwen, Gemma, Llama, Mistral, Aya) and report the cheapest that passes every case.",
          "- Add negation/other-person handling as a tool-side check, and a second-reader pass for discriminators.",
          "- Paediatric TEWS; a clinician review of the discriminator catalogue; a pilot on de-identified retrospective data under ethics approval."]
    (ROOT / "EVALS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("Wrote EVALS.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", choices=["scoring", "agent"])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--write-report", action="store_true")
    a = ap.parse_args()
    if a.layer == "scoring":
        r = layer_scoring()
        (ROOT / "evals" / "results_scoring.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
        for c in r["cases"]:
            print(("PASS " if c["pass"] else "FAIL ") + c["id"] + "  " + c["desc"])
            for k in c["checks"]:
                if not k["ok"]:
                    print(f"      x {k['name']}  {k['detail']}")
        for g in r["guards"]:
            print(("PASS " if g["pass"] else "FAIL ") + g["name"])
        print(f"\nCases {r['passed']}/{r['total']}   Guards {r['guards_passed']}/{r['guards_total']}")
    elif a.layer == "agent":
        print(f"Live agent eval: model {config.model_name()} via {config.provider()}")
        r = layer_agent(a.repeat)
        (ROOT / "evals" / "results_agent.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
        print(f"\nCases passing every run: {r['passed']}/{r['total']}")
    if a.write_report:
        write_report()


if __name__ == "__main__":
    main()
