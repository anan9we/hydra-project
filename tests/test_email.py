import base64
import email
import json

from triage_desk import analytics, db, emailer, gate
from triage_desk.runner import run_triage
from tests.test_agent_wiring import good_p001


class FakeResp:
    ok, status_code, text = True, 200, "{}"


def decode_raw(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def use_oauth(monkeypatch, tmp_path, calls):
    (tmp_path / "token.json").write_text("{}")
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setenv("ALERT_EMAIL_TO", "charge.nurse@example.com")
    monkeypatch.setattr(emailer, "_access_token", lambda: "ya29.fake")
    monkeypatch.setattr(emailer.requests, "post", lambda url, **kw: (calls.append((url, kw)), FakeResp())[1])


def test_oauth_send_uses_gmail_api_with_bearer_token(tdb, monkeypatch, tmp_path):
    calls = []
    use_oauth(monkeypatch, tmp_path, calls)
    assert emailer.send_email("hello", "body", attachments={"a.csv": "x,y\n1,2\n"}) == "oauth"
    url, kw = calls[0]
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    assert kw["headers"]["Authorization"] == "Bearer ya29.fake"
    msg = decode_raw(kw["json"]["raw"])
    assert msg["To"] == "charge.nurse@example.com" and msg["Subject"] == "hello"
    assert any(p.get_filename() == "a.csv" for p in msg.walk())


def test_scope_is_send_only():
    assert emailer.SCOPES == ["https://www.googleapis.com/auth/gmail.send"]


def test_smtp_app_password_fallback(tdb, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): sent["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, m): sent["msg"] = m
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com"); monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdabcdabcdabcd")
    monkeypatch.setenv("ALERT_EMAIL_TO", "x@example.com")
    monkeypatch.setattr(emailer.smtplib, "SMTP", FakeSMTP)
    assert emailer.send_email("s", "b") == "smtp"
    assert sent["host"] == ("smtp.gmail.com", 587) and sent["tls"] and sent["login"][0] == "me@gmail.com"


def test_not_configured_gives_readable_error(tdb, monkeypatch):
    import pytest
    monkeypatch.setenv("ALERT_EMAIL_TO", "x@example.com")
    with pytest.raises(RuntimeError, match="gmail-auth"):
        emailer.send_email("s", "b")


def test_alert_after_run_is_deidentified(tdb, monkeypatch, tmp_path):
    calls = []
    use_oauth(monkeypatch, tmp_path, calls)
    r = run_triage("P-001", model=good_p001())
    assert r["ok"] and r["email"] == "sent via oauth"
    msg = decode_raw(calls[0][1]["json"]["raw"])
    text = msg.get_payload(decode=True).decode() + msg["Subject"]
    assert "awaiting clinician approval" in msg["Subject"] and "Orange" in msg["Subject"]
    assert db.ticket("P-001") in text
    for forbidden in ("P-001", "Synthetic", "chest", "Chest", "kifua", "112", "resp_rate", "heart"):
        assert forbidden not in text, forbidden


def test_email_failure_never_blocks_triage(tdb, monkeypatch, tmp_path):
    calls = []
    use_oauth(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(emailer.requests, "post", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("offline")))
    r = run_triage("P-001", model=good_p001())
    assert r["ok"] and r["email"].startswith("failed")
    assert db.one("SELECT COUNT(*) n FROM proposals")["n"] == 1


def test_digest_and_audit_csv_deidentified(tdb):
    r = run_triage("P-001", model=good_p001())
    gate.decide(r["proposal_ids"][0], "Dr Test", "4321", "approved", note="ok")
    subj, body, s = analytics.build_digest(24)
    assert s["approved"] == 1 and "Audit chain intact" in body
    csv_text = analytics.audit_csv()
    assert db.ticket("P-001") in csv_text and "P-001" not in csv_text and "Chest" not in csv_text
