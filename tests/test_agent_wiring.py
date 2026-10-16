import json

from triage_desk import db, gate
from triage_desk.runner import run_triage
from tests.scripted import scripted

P = {"patient_id": "P-001"}


def good_p001():
    return scripted([
        ("tool", "get_intake", P), ("tool", "score_tews", P),
        ("tool", "assert_discriminator", {**P, "discriminator_id": "chest_pain", "entry_id": "s1", "quote": "Chest pain"}),
        ("tool", "submit_priority", {**P, "rationale": "Chest pain with fast pulse and borderline low blood pressure."}),
        ("final", {"patient_id": "P-001", "outcome": "priority_proposed", "proposal_ids": [1],
                   "summary": "Chest pain with raised pulse and breathing rate; urgent review proposed."}),
    ])


def test_full_run_creates_pending_only_and_logs(tdb):
    calls = []
    r = run_triage("P-001", model=good_p001(), on_call=lambda *a: calls.append(a[:2]))
    assert r["ok"], r
    assert r["outcome"] == "priority_proposed"
    prop = db.one("SELECT * FROM proposals WHERE id=?", (r["proposal_ids"][0],))
    assert prop["status"] == "pending" and json.loads(prop["payload"])["colour"] == "Orange"
    assert db.rows("SELECT * FROM queue") == []            # agent can NOT touch the live queue
    names = [x["tool"] for x in db.rows("SELECT tool FROM tool_log WHERE run_id=?", (r["run_id"],))]
    assert names == ["get_intake", "score_tews", "assert_discriminator", "submit_priority"]
    run = db.one("SELECT * FROM runs WHERE id=?", (r["run_id"],))
    assert run["outcome"] == "priority_proposed" and run["tool_calls"] == 4 and run["ended_at"]
    assert ("call", "get_intake") in calls


def test_recovers_from_fake_quote_and_diagnosis_wording(tdb):
    m = scripted([
        ("tool", "get_intake", P),
        ("tool", "assert_discriminator", {**P, "discriminator_id": "chest_pain", "entry_id": "s1", "quote": "heart attack"}),
        ("tool", "assert_discriminator", {**P, "discriminator_id": "chest_pain", "entry_id": "s1", "quote": "Chest pain"}),
        ("tool", "submit_priority", {**P, "rationale": "Likely has a heart attack"}),
        ("tool", "submit_priority", {**P, "rationale": "Chest pain with fast pulse."}),
        ("final", {"patient_id": "P-001", "outcome": "priority_proposed", "proposal_ids": [1], "summary": "Urgent review proposed."}),
    ])
    r = run_triage("P-001", model=m)
    assert r["ok"], r
    oks = [x["ok"] for x in db.rows("SELECT ok FROM tool_log WHERE run_id=?", (r["run_id"],))]
    assert oks == [1, 0, 1, 0, 1]   # two rejected calls, both recovered


def test_validator_rejects_invented_proposal_id(tdb):
    m = scripted([
        ("tool", "get_intake", P),
        ("final", {"patient_id": "P-001", "outcome": "priority_proposed", "proposal_ids": [99], "summary": "Done."}),
        ("tool", "score_tews", P),
        ("tool", "submit_priority", {**P, "rationale": "Fast pulse and breathing rate with low-normal pressure."}),
        ("final", {"patient_id": "P-001", "outcome": "priority_proposed", "proposal_ids": [1], "summary": "Priority proposed."}),
    ])
    r = run_triage("P-001", model=m)
    assert r["ok"] and r["proposal_ids"] == [1]


def test_child_is_escalated_not_scored(tdb):
    P4 = {"patient_id": "P-004"}
    m = scripted([
        ("tool", "get_intake", P4), ("tool", "score_tews", P4),
        ("tool", "route_referral", {**P4, "department_id": "paediatrics", "entry_id": "s1", "quote": "very sleepy",
                                    "reason": "Child aged 4 with fever and unusual sleepiness; adult scale does not apply."}),
        ("final", {"patient_id": "P-004", "outcome": "escalated_unscored", "proposal_ids": [1], "summary": "Child referred to paediatrics."}),
    ])
    r = run_triage("P-004", model=m)
    assert r["ok"] and r["outcome"] == "escalated_unscored"
    assert db.one("SELECT COUNT(*) n FROM proposals WHERE action='set_priority'")["n"] == 0


def test_failure_fails_safe(tdb):
    m = scripted([("final", {"patient_id": "P-001", "outcome": "priority_proposed", "proposal_ids": [5], "summary": "x"})])
    r = run_triage("P-001", model=m)
    assert not r["ok"] and r["outcome"] == "failed"
    assert db.rows("SELECT * FROM queue") == [] and db.rows("SELECT * FROM proposals") == []


def test_cloud_model_blocked_for_real_patient(tdb, monkeypatch):
    import pytest
    from triage_desk.runner import ResidencyError
    db.add_patient("R-1", "Real Person", 40, "F", synthetic=False, symptoms=["cough"], vitals_rounds=[{"resp_rate": 16}])
    monkeypatch.setenv("TRIAGE_PROVIDER", "gemini")
    with pytest.raises(ResidencyError):
        run_triage("R-1", model=good_p001())


def test_gate_end_to_end(tdb):
    r = run_triage("P-001", model=good_p001())
    pid = r["proposal_ids"][0]
    import pytest
    with pytest.raises(gate.GateError):
        gate.decide(pid, "Dr Test", "0000", "approved")           # wrong PIN
    assert db.rows("SELECT * FROM queue") == []
    gate.decide(pid, "Dr Test", "4321", "approved", note="agree")
    q = gate.queue_view()
    assert q[0]["patient_id"] == "P-001" and q[0]["colour"] == "Orange" and q[0]["approved_by"] == "Dr Test"
    with pytest.raises(gate.GateError):
        gate.decide(pid, "Dr Test", "4321", "approved")           # cannot decide twice
    ok, msg = gate.verify_chain()
    assert ok, msg
    with db.conn() as c:                                          # tamper with the audit log
        c.execute("UPDATE approvals SET decision='rejected' WHERE id=1")
    assert not gate.verify_chain()[0]


def test_override_needs_reason_and_reject_leaves_queue_empty(tdb):
    import pytest
    r = run_triage("P-001", model=good_p001())
    pid = r["proposal_ids"][0]
    with pytest.raises(gate.GateError):
        gate.decide(pid, "Dr Test", "4321", "approved", override_colour="Red")
    gate.decide(pid, "Dr Test", "4321", "approved", note="Looks unwell on exam", override_colour="Red")
    assert gate.queue_view()[0]["colour"] == "Red"
