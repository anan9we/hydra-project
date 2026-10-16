import json

from triage_desk import db, evals
from triage_desk.runner import run_triage
from tests.scripted import scripted


def test_scoring_layer_all_pass():
    r = evals.layer_scoring()
    assert r["passed"] == r["total"] == 18 and r["guards_passed"] == r["guards_total"]


def test_cases_have_required_labels():
    cases = evals.load_cases()
    assert len(cases) >= 10 and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert {"outcome", "colour", "tews", "discriminators", "recheck", "referral", "no_priority"} <= set(c["expect"])


def test_agent_judge_distinguishes_right_from_wrong(tdb):
    """Plumbing check for the live-agent judge using a scripted model (NOT a model-quality result)."""
    c = next(x for x in evals.load_cases() if x["id"] == "E02")
    evals.seed_case(c)
    P = {"patient_id": "E02"}
    good = scripted([("tool", "get_intake", P), ("tool", "score_tews", P),
                     ("tool", "assert_discriminator", {**P, "discriminator_id": "chest_pain", "entry_id": "s1", "quote": "Chest pain"}),
                     ("tool", "submit_priority", {**P, "rationale": "Chest pain with raised pulse and breathing rate."}),
                     ("final", {"patient_id": "E02", "outcome": "priority_proposed", "proposal_ids": [1], "summary": "Urgent review proposed."})])
    r = run_triage("E02", model=good)
    ck = evals.judge_agent_case(c, r, r["run_id"])
    assert all(ok for _, ok, _ in ck), ck
    # a run that skips the discriminator must be judged as failing
    evals.seed_case({**c, "id": "E02b"})
    P2 = {"patient_id": "E02b"}
    lazy = scripted([("tool", "get_intake", P2), ("tool", "score_tews", P2),
                     ("tool", "submit_priority", {**P2, "rationale": "Raised pulse and breathing rate."}),
                     ("final", {"patient_id": "E02b", "outcome": "priority_proposed", "proposal_ids": [2], "summary": "Review proposed."})])
    r2 = run_triage("E02b", model=lazy)
    ck2 = evals.judge_agent_case({**c, "id": "E02b"}, r2, r2["run_id"])
    assert not all(ok for _, ok, _ in ck2)
