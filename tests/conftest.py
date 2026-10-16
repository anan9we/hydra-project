import pytest

from triage_desk import db, gate, seed


@pytest.fixture()
def tdb(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("TRIAGE_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("TRIAGE_RUN_ID", raising=False)
    for k in ("ALERT_EMAIL_TO", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):   # tests must never send real e-mail
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "no_token.json"))
    seed.seed_demo()
    gate.add_clinician("Dr Test", "Medical Officer", "4321")
    return tmp_path / "t.db"
