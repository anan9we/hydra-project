"""One-time Gmail sign-in:  python -m triage_desk gmail-auth
Opens your browser, you approve 'send email on your behalf', and token.json is saved (never commit it).
Needs credentials.json: Google Cloud Console > APIs & Services > Credentials > OAuth client ID > Desktop app."""
from google_auth_oauthlib.flow import InstalledAppFlow

from . import emailer


def main() -> None:
    cf = emailer.credentials_file()
    if not cf.exists():
        raise SystemExit(f"Missing {cf}. See README section 'Gmail setup' (5 minutes).")
    flow = InstalledAppFlow.from_client_secrets_file(str(cf), emailer.SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    emailer.token_file().write_text(creds.to_json())
    print(f"Saved {emailer.token_file()}. Test it with: python -m triage_desk email-test")


if __name__ == "__main__":
    main()
