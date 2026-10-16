"""Gmail sending via OAuth2 (Gmail API, gmail.send scope only). Falls back to an app password over SMTP.

PRIVACY: e-mail leaves the country (Google servers). So messages carry NO names, vitals or complaint text:
only counts, colours and pseudonymous ticket ids. Details stay in the local web app."""
import base64
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests

from . import config, db

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def _file(env_key: str, default: str) -> Path:
    p = Path(os.getenv(env_key, default))
    return p if p.is_absolute() else config.ROOT / p


def token_file() -> Path:
    return _file("GMAIL_TOKEN_FILE", "token.json")


def credentials_file() -> Path:
    return _file("GMAIL_CREDENTIALS_FILE", "credentials.json")


def method() -> str | None:
    if token_file().exists():
        return "oauth"
    if os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"):
        return "smtp"
    return None


def _access_token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(token_file()), SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
            token_file().write_text(creds.to_json())
        else:
            raise RuntimeError("Gmail token expired and cannot refresh. Run: python -m triage_desk gmail-auth")
    return creds.token


def _build(subject, body, to, sender, attachments):
    m = EmailMessage()
    m["To"], m["Subject"] = to, subject
    if sender:
        m["From"] = sender
    m.set_content(body)
    for name, text in (attachments or {}).items():
        m.add_attachment(text.encode(), maintype="text", subtype="csv", filename=name)
    return m


def send_email(subject: str, body: str, to: str | None = None, attachments: dict | None = None) -> str:
    """Returns the method used ('oauth' or 'smtp'). Raises RuntimeError with a readable reason."""
    to = to or os.getenv("ALERT_EMAIL_TO")
    if not to:
        raise RuntimeError("Set ALERT_EMAIL_TO in .env")
    how = method()
    if how == "oauth":
        msg = _build(subject, body, to, None, attachments)  # Gmail fills in From = the authorised account
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = requests.post(SEND_URL, headers={"Authorization": f"Bearer {_access_token()}"},
                          json={"raw": raw}, timeout=20)
        if not r.ok:
            raise RuntimeError(f"Gmail API {r.status_code}: {r.text[:200]}")
        return "oauth"
    if how == "smtp":
        user, pwd = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
        msg = _build(subject, body, to, user, attachments)
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return "smtp"
    raise RuntimeError("Gmail not set up. Run: python -m triage_desk gmail-auth (see README)")


def alert_body(new_proposal_ids: list[int]) -> tuple[str, str]:
    """De-identified alert: counts, colours, pseudonymous tickets. Nothing else."""
    pend = db.rows("SELECT id, patient_id, action, payload FROM proposals WHERE status='pending' ORDER BY id")
    import json
    lines, highest = [], None
    from .sats import RANK
    for p in pend:
        pl = json.loads(p["payload"])
        col = pl.get("colour")
        if col and (highest is None or RANK[col] > RANK[highest]):
            highest = col
        label = {"set_priority": f"priority {col}", "recheck_flag": "re-check", "referral": "referral"}[p["action"]]
        lines.append(f"  {db.ticket(p['patient_id'])}: {label}" + (" (new)" if p["id"] in new_proposal_ids else ""))
    subj = f"[Triage Desk] {len(pend)} proposal(s) awaiting clinician approval" + (f" - highest {highest}" if highest else "")
    body = (f"{len(pend)} proposal(s) are waiting for a clinician in the Triage Desk web app.\n\n"
            + "\n".join(lines) + "\n\nOpen the Triage Desk on the hospital machine to review the cited evidence and approve "
            "or reject. This message deliberately contains no patient names, vitals or complaint text.\n")
    return subj, body


def alert_pending(new_proposal_ids: list[int]) -> str:
    """Called after an agent run. Never raises: e-mail trouble must not block triage."""
    if os.getenv("TRIAGE_ALERT_EMAILS", "1") != "1" or not os.getenv("ALERT_EMAIL_TO") or method() is None:
        return "skipped"
    try:
        s, b = alert_body(new_proposal_ids)
        return "sent via " + send_email(s, b)
    except Exception as e:  # noqa: BLE001
        return f"failed: {e}"
