"""Command line. Examples:
  python -m triage_desk setup              # create db, 6 synthetic patients, 2 demo clinicians
  python -m triage_desk triage P-001       # run the agent live (tool calls printed as they happen)
  python -m triage_desk pending            # proposals waiting for a clinician
  python -m triage_desk approve 1 --clinician "Dr. Wanjiru Kamau" --pin 1234
  python -m triage_desk queue | audit
"""
import argparse
import getpass
import json
import sys

from . import config, db, gate, seed

DEMO_CLINICIANS = [("Dr. Wanjiru Kamau", "Medical Officer", "1234"), ("Nurse Otieno", "Nurse-in-charge", "5678")]
G, Y, R, C, D, X = "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[2m", "\033[0m"
COL = {"Red": R, "Orange": Y, "Yellow": Y, "Green": G, "Unscored": C}


def cmd_setup(a):
    n = seed.seed_demo()
    for name, role, pin in DEMO_CLINICIANS:
        gate.add_clinician(name, role, pin)
    print(f"Created {config.db_path()} with {n} SYNTHETIC patients and {len(DEMO_CLINICIANS)} demo clinicians "
          f"(demo PINs: {', '.join(p for _, _, p in DEMO_CLINICIANS)}).")


def _trace(kind, name, payload):
    if kind == "call":
        args = {k: v for k, v in payload.items() if k != "patient_id"}
        print(f"  {C}> {name}{X}({json.dumps(args, ensure_ascii=False)[:150]})")
    elif kind == "result":
        print(f"  {G}< {str(payload)[:170]}{X}")
    else:
        print(f"  {R}! rejected: {str(payload)[:230]}{X}")


def cmd_triage(a):
    from .runner import run_triage
    print(f"{D}model: {config.model_name()} via {config.provider()} "
          f"({'CLOUD - synthetic patients only' if config.is_cloud_llm() else 'LOCAL - data stays on this machine'}){X}")
    for pid in a.patients:
        print(f"\n=== Triage {pid} ===")
        r = run_triage(pid, on_call=_trace)
        if r["ok"]:
            print(f"{G}Outcome: {r['outcome']}{X}  proposals: {r['proposal_ids']}\n  {r['summary']}")
        else:
            print(f"{R}Run failed safely (no proposal created): {r['error']}{X}")
    print(f"\nNext: python -m triage_desk pending")


def cmd_pending(a):
    for p in gate.pending_view():
        pl = p["payload"]
        head = {"set_priority": f"PRIORITY {pl.get('colour')} (TEWS {pl.get('tews')}"
                                f"{', PROVISIONAL' if pl.get('provisional') else ''})",
                "recheck_flag": f"RE-CHECK in {pl.get('minutes')} min", "referral": f"REFER to {pl.get('department')}"}[p["action"]]
        print(f"\n#{p['id']}  {p['patient_id']}  {COL.get(pl.get('colour'), '')}{head}{X}\n  why: {p['rationale']}")
        for e in p["evidence"]:
            cite = e.get("quote") or e.get("note") or f"{e.get('field')}={e.get('value')} ({e.get('points')} pts)"
            print(f"  - [{e['entry_id']}] {cite}")
    if not gate.pending_view():
        print("Nothing pending.")


def cmd_approve(a):
    pin = a.pin or getpass.getpass("PIN: ")
    try:
        r = gate.decide(a.proposal_id, a.clinician, pin, "rejected" if a.reject else "approved", a.note or "", a.override)
        print(f"{G}{r['decision']} by {r['by']}{X}")
    except gate.GateError as e:
        print(f"{R}{e}{X}")
        sys.exit(1)


def cmd_queue(a):
    for i, q in enumerate(gate.queue_view(), 1):
        print(f"{i}. {COL[q['colour']]}{q['colour']:<8}{X} {q['patient_id']}  TEWS={q['tews']}  approved by {q['approved_by']}"
              f"  re-check due {q['recheck_due']}  {('-> ' + q['referral']) if q['referral'] else ''}")
    if not gate.queue_view():
        print("Live queue is empty (nothing approved yet).")


def cmd_audit(a):
    ok, msg = gate.verify_chain()
    print((G if ok else R) + msg + X)
    for r in db.rows("SELECT * FROM approvals ORDER BY id"):
        print(f"#{r['id']} {r['decided_at']} proposal {r['proposal_id']} {r['decision']} by {r['clinician']}"
              f"{' OVERRIDE ' + r['override_value'] if r['override_value'] else ''} {r['note'] or ''}")


def cmd_web(a):
    from .web import serve
    serve(a.port)


def cmd_gmail_auth(a):
    from .gmail_auth import main as m
    m()


def cmd_email_test(a):
    from . import emailer
    try:
        how = emailer.send_email("[Triage Desk] test e-mail", "Gmail sending works. This is a test with no patient data.")
        print(f"{G}Sent via {how} to {__import__('os').getenv('ALERT_EMAIL_TO')}{X}")
    except Exception as e:  # noqa: BLE001
        print(f"{R}{e}{X}")
        sys.exit(1)


def cmd_digest(a):
    from . import analytics, emailer
    subj, body, _ = analytics.build_digest(a.hours)
    print(body)
    if a.send:
        print(f"{G}Sent via {emailer.send_email(subj, body, attachments={'audit_log_deidentified.csv': analytics.audit_csv()})}{X}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="triage_desk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    t = sub.add_parser("triage"); t.add_argument("patients", nargs="+"); t.set_defaults(fn=cmd_triage)
    sub.add_parser("pending").set_defaults(fn=cmd_pending)
    ap_ = sub.add_parser("approve"); ap_.add_argument("proposal_id", type=int); ap_.add_argument("--clinician", required=True)
    ap_.add_argument("--pin"); ap_.add_argument("--reject", action="store_true"); ap_.add_argument("--note")
    ap_.add_argument("--override", choices=["Green", "Yellow", "Orange", "Red"]); ap_.set_defaults(fn=cmd_approve)
    sub.add_parser("queue").set_defaults(fn=cmd_queue)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    w = sub.add_parser("web"); w.add_argument("--port", type=int, default=8000); w.set_defaults(fn=cmd_web)
    sub.add_parser("gmail-auth").set_defaults(fn=cmd_gmail_auth)
    sub.add_parser("email-test").set_defaults(fn=cmd_email_test)
    d = sub.add_parser("digest"); d.add_argument("--hours", type=int, default=24); d.add_argument("--send", action="store_true"); d.set_defaults(fn=cmd_digest)
    a = ap.parse_args(argv)
    db.init()
    a.fn(a)


if __name__ == "__main__":
    main()
