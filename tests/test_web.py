import re

import pytest

from triage_desk import db, gate, web
from triage_desk.runner import run_triage
from tests.test_agent_wiring import good_p001


@pytest.fixture()
def client(tdb):
    web.app.config["TESTING"] = True
    return web.app.test_client()


def token(c, path="/"):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([0-9a-f]+)"', html).group(1)


def test_pages_render(client):
    for path in ("/", "/approvals", "/runs", "/audit", "/intake", "/patients/P-001"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert b"local: patient data stays on this machine" in client.get("/").data


def test_post_without_csrf_rejected(client):
    assert client.post("/approvals/1", data={"pin": "4321"}).status_code == 400


def test_approval_flow_via_web(client):
    r = run_triage("P-001", model=good_p001())
    pid = r["proposal_ids"][0]
    assert b"Priority" in client.get("/approvals").data and b"Chest pain" in client.get("/approvals").data
    t = token(client, "/approvals")
    bad = client.post(f"/approvals/{pid}", data={"_csrf": t, "clinician": "Dr Test", "pin": "0000", "decision": "approved"}, follow_redirects=True)
    assert b"wrong PIN" in bad.data and db.rows("SELECT * FROM queue") == []
    ok = client.post(f"/approvals/{pid}", data={"_csrf": t, "clinician": "Dr Test", "pin": "4321", "decision": "approved"}, follow_redirects=True)
    assert b"approved by Dr Test" in ok.data
    assert gate.queue_view()[0]["colour"] == "Orange"
    assert b"Audit chain intact" in client.get("/audit").data


def test_new_intake_and_vitals_validation(client):
    t = token(client, "/intake")
    r = client.post("/intake", data={"_csrf": t, "name": "Syn Z", "age": "30", "sex": "F", "note": "Headache",
                                     "resp_rate": "999", "synthetic": "on"}, follow_redirects=True)
    assert b"plausible range" in r.data
    r = client.post("/intake", data={"_csrf": t, "name": "Syn Z", "age": "30", "sex": "F", "note": "Headache",
                                     "resp_rate": "16", "synthetic": "on"}, follow_redirects=True)
    assert b"Saved P-007" in r.data
    t2 = token(client, "/patients/P-007")
    r = client.post("/patients/P-007", data={"_csrf": t2, "heart_rate": "88"}, follow_redirects=True)
    assert b"Vitals round 2 saved" in r.data


def test_html_is_escaped(client):
    db.add_patient("P-099", "<script>alert(1)</script>", 30, "F", True, ["<img src=x onerror=alert(1)>"], [{"resp_rate": 16}])
    html = client.get("/patients/P-099").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html
