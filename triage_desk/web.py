"""Local web app for the triage desk. Binds to 127.0.0.1 only (patient data stays on this machine)."""
import json
import os
import secrets
import threading

from flask import Flask, abort, flash, redirect, render_template_string, request, session, url_for

from . import analytics, config, db, gate, sats

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(16)
COL = {"Red": "#b91c1c", "Orange": "#c2410c", "Yellow": "#a16207", "Green": "#15803d", "Unscored": "#0369a1"}

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Triage Desk</title>{% if refresh %}<meta http-equiv="refresh" content="3">{% endif %}
<style>
:root{--bg:#f8fafc;--fg:#0f172a;--mut:#475569;--card:#fff;--bd:#cbd5e1;--acc:#0f766e}
@media (prefers-color-scheme:dark){:root{--bg:#0f172a;--fg:#e2e8f0;--mut:#94a3b8;--card:#1e293b;--bd:#334155;--acc:#2dd4bf}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}
header{padding:12px 20px;border-bottom:1px solid var(--bd);display:flex;gap:18px;flex-wrap:wrap;align-items:center}
header b{font-size:18px} a{color:var(--acc)} main{max-width:1050px;margin:0 auto;padding:16px 20px 60px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin:14px 0}
table{width:100%;border-collapse:collapse} th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd);vertical-align:top}
.pill{display:inline-block;color:#fff;border-radius:99px;padding:1px 10px;font-weight:600;font-size:13px}
.banner{padding:8px 14px;border-radius:8px;font-size:13px;margin:10px 0}.ok{background:#dcfce7;color:#14532d}.warn{background:#fee2e2;color:#7f1d1d}
.msg{background:#e0f2fe;color:#0c4a6e;padding:8px 14px;border-radius:8px;margin:8px 0}
input,select,textarea,button{font:inherit;padding:5px 8px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg)}
button{cursor:pointer;background:var(--acc);color:#fff;border:0} button.sec{background:#64748b} button.no{background:#b91c1c}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px} small,.mut{color:var(--mut)}
</style></head><body>
<header><b>Triage Desk</b><a href="{{ url_for('home') }}">Queue</a><a href="{{ url_for('approvals') }}">Approvals{% if npend %} ({{ npend }}){% endif %}</a>
<a href="{{ url_for('runs') }}">Agent runs</a><a href="{{ url_for('audit') }}">Audit log</a><a href="{{ url_for('new_patient') }}">New intake</a></header>
<main>
<div class="banner {{ 'warn' if cloud else 'ok' }}">Model: {{ model }} ({{ 'CLOUD: synthetic patients only' if cloud else 'local: patient data stays on this machine' }}).
 Prototype on synthetic data. The agent only proposes; a named clinician decides.</div>
{% for m in get_flashed_messages() %}<div class="msg">{{ m }}</div>{% endfor %}
{{ body|safe }}</main></body></html>"""


def page(body_tpl: str, refresh=False, **ctx):
    body = render_template_string(body_tpl, colours=COL, csrf=csrf(), **ctx)
    return render_template_string(BASE, body=body, refresh=refresh, model=config.model_name(),
                                  cloud=config.is_cloud_llm(),
                                  npend=db.one("SELECT COUNT(*) n FROM proposals WHERE status='pending'")["n"])


def csrf() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent, expected = request.form.get("_csrf"), session.get("csrf")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Bad CSRF token (reload the page)")


@app.route("/")
def home():
    patients = db.rows("SELECT p.*, (SELECT COUNT(*) FROM proposals x WHERE x.patient_id=p.id AND x.status='pending') pend, "
                       "(SELECT colour FROM queue q WHERE q.patient_id=p.id) colour FROM patients p ORDER BY p.arrived_at")
    s = analytics.stats(24)
    return page("""
<div class="card"><h3>Live queue <small>(only clinician-approved priorities appear here)</small></h3>
{% if queue %}<table><tr><th>#</th><th>Priority</th><th>Patient</th><th>TEWS</th><th>Approved by</th><th>Re-check due</th><th>Referral</th></tr>
{% for q in queue %}<tr><td>{{ loop.index }}</td><td><span class="pill" style="background:{{ colours[q.colour] }}">{{ q.colour }}</span>{% if q.provisional %} <small>provisional</small>{% endif %}</td>
<td>{{ q.patient_id }} <small>{{ q.display_name }}, {{ q.age }}{{ q.sex }}</small></td><td>{{ q.tews if q.tews is not none else '-' }}</td><td>{{ q.approved_by }}</td>
<td>{{ q.recheck_due or '-' }}</td><td>{{ q.referral or '-' }}</td></tr>{% endfor %}</table>{% else %}<p class="mut">Empty. Run the agent on a patient, then approve its proposal.</p>{% endif %}
{% if overdue %}<div class="banner warn">{{ overdue|length }} re-check(s) overdue: {{ overdue|map(attribute='patient_id')|join(', ') }}</div>{% endif %}</div>
<div class="card"><h3>Patients</h3><table><tr><th>ID</th><th>Name</th><th>Age/Sex</th><th>Data</th><th>Status</th><th></th></tr>
{% for p in patients %}<tr><td><a href="{{ url_for('patient', pid=p.id) }}">{{ p.id }}</a></td><td>{{ p.display_name }}</td><td>{{ p.age }}{{ p.sex }}</td>
<td>{{ 'synthetic' if p.synthetic else 'REAL' }}</td><td>{% if p.colour %}<span class="pill" style="background:{{ colours[p.colour] }}">{{ p.colour }}</span>{% elif p.pend %}{{ p.pend }} pending{% else %}waiting{% endif %}</td>
<td><form method="post" action="{{ url_for('run_agent', pid=p.id) }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button>Run triage agent</button></form></td></tr>{% endfor %}</table></div>
<div class="card"><h3>Last 24 h</h3><p class="mut">{{ s.runs }} runs ({{ s.runs_failed }} failed safely) · avg {{ s.avg_tool_calls }} tool calls, {{ s.avg_tokens }} tokens · cost ${{ s.total_cost_usd }} ·
{{ s.approved }} approved, {{ s.rejected }} rejected, {{ s.overrides }} overrides · median {{ s.median_minutes_to_decision }} min to decision</p>
<form method="post" action="{{ url_for('send_digest') }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="sec">E-mail shift digest (de-identified)</button></form></div>
""", queue=gate.queue_view(), patients=patients, s=s, overdue=analytics.overdue_rechecks())


@app.route("/patients/<pid>", methods=["GET", "POST"])
def patient(pid):
    p = db.one("SELECT * FROM patients WHERE id=?", (pid,))
    if not p:
        abort(404)
    if request.method == "POST":
        vit, bad = _read_vitals(request.form)
        if bad:
            flash(bad)
        elif vit:
            rnd = db.add_vitals_round(pid, vit)
            flash(f"Vitals round {rnd} saved. Run the agent again to re-assess (it will compare with the previous round).")
        return redirect(url_for("patient", pid=pid))
    ents = db.rows("SELECT * FROM entries WHERE patient_id=? ORDER BY kind DESC, round, entry_id", (pid,))
    props = db.rows("SELECT id, action, status, created_at FROM proposals WHERE patient_id=? ORDER BY id DESC", (pid,))
    return page("""
<div class="card"><h3>{{ p.id }} · {{ p.display_name }} <small>{{ p.age }}{{ p.sex }} · {{ 'synthetic' if p.synthetic else 'REAL patient' }}</small></h3>
<table><tr><th>Entry</th><th>Recorded</th></tr>{% for e in ents %}<tr><td><code>{{ e.entry_id }}</code></td>
<td>{% if e.kind == 'symptom' %}{{ e.text }}{% else %}{{ e.field }} = {{ e.value }}{% endif %}</td></tr>{% endfor %}</table></div>
<div class="card"><h3>Add a vitals round (re-check)</h3><form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}">{{ vitals_form|safe }}<p><button>Save vitals</button> <small>Leave blank what was not measured.</small></p></form></div>
<div class="card"><h3>Proposals</h3>{% for x in props %}<div>#{{ x.id }} {{ x.action }} · {{ x.status }} · {{ x.created_at }}</div>{% else %}<p class="mut">None yet.</p>{% endfor %}
<form method="post" action="{{ url_for('run_agent', pid=p.id) }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button>Run triage agent</button></form></div>
""", p=p, ents=ents, props=props, vitals_form=VITALS_FORM)


VITALS_FORM = """<div class="grid">
<label>Mobility<br><select name="mobility"><option value="">-</option><option>walking</option><option>with_help</option><option>immobile</option></select></label>
<label>Resp rate<br><input name="resp_rate" inputmode="numeric" size="5"></label><label>Heart rate<br><input name="heart_rate" inputmode="numeric" size="5"></label>
<label>Systolic BP<br><input name="sbp" inputmode="numeric" size="5"></label><label>Temp C<br><input name="temp_c" inputmode="decimal" size="5"></label>
<label>AVPU<br><select name="avpu"><option value="">-</option><option>A</option><option>V</option><option>P</option><option>U</option></select></label>
<label>Trauma<br><select name="trauma"><option value="">-</option><option>no</option><option>yes</option></select></label></div>"""

RANGES = {"resp_rate": (0, 80), "heart_rate": (0, 300), "sbp": (0, 300), "temp_c": (25, 45)}


def _read_vitals(form) -> tuple[dict, str | None]:
    vit = {}
    for f in sats.FIELDS:
        v = (form.get(f) or "").strip()
        if not v:
            continue
        if f in RANGES:
            try:
                x = float(v)
            except ValueError:
                return {}, f"{f}: '{v}' is not a number"
            lo, hi = RANGES[f]
            if not lo <= x <= hi:
                return {}, f"{f}: {x} is outside the plausible range {lo}-{hi}; please re-measure"
        vit[f] = v
    return vit, None


@app.route("/intake", methods=["GET", "POST"])
def new_patient():
    if request.method == "POST":
        f = request.form
        try:
            age = int(f.get("age", ""))
            assert 0 <= age <= 120
        except (ValueError, AssertionError):
            flash("Age must be a number 0-120")
            return redirect(url_for("new_patient"))
        vit, bad = _read_vitals(f)
        if bad:
            flash(bad)
            return redirect(url_for("new_patient"))
        note = (f.get("note") or "").strip()
        if not note:
            flash("Enter the complaint / nurse note")
            return redirect(url_for("new_patient"))
        pid = db.next_patient_id()
        db.add_patient(pid, (f.get("name") or "").strip() or pid, age, f.get("sex") or "", f.get("synthetic") == "on",
                       [note], [vit] if vit else [])
        flash(f"Saved {pid}.")
        return redirect(url_for("patient", pid=pid))
    return page("""<div class="card"><h3>New intake (triage nurse)</h3><form method="post"><input type="hidden" name="_csrf" value="{{ csrf }}">
<p><label>Name <input name="name"></label> <label>Age <input name="age" size="4" required></label> <label>Sex <select name="sex"><option>F</option><option>M</option></select></label></p>
<p><label>Complaint / nurse note (English or Kiswahili)<br><textarea name="note" rows="3" cols="70" required></textarea></label></p>{{ vitals_form|safe }}
<p><label><input type="checkbox" name="synthetic" checked> This is a SYNTHETIC patient (untick only for real patients; real patients are blocked from cloud models)</label></p>
<button>Save intake</button></form></div>""", vitals_form=VITALS_FORM)


@app.route("/run/<pid>", methods=["POST"])
def run_agent(pid):
    from .runner import run_triage

    def job():
        run_triage(pid)
    threading.Thread(target=job, daemon=True).start()
    flash(f"Agent started for {pid}. Watch the tool calls below; the proposal will appear under Approvals.")
    return redirect(url_for("runs"))


@app.route("/runs")
def runs():
    rs = db.rows("SELECT * FROM runs ORDER BY id DESC LIMIT 8")
    for r in rs:
        r["log"] = db.rows("SELECT * FROM tool_log WHERE run_id=? ORDER BY id", (r["id"],))
    return page("""{% for r in rs %}<div class="card"><b>Run {{ r.id }}</b> · {{ r.patient_id }} · {{ r.model }} ({{ r.provider }}) ·
{% if r.ended_at %}{{ r.outcome }} · {{ r.tool_calls }} tool calls · {{ r.input_tokens + r.output_tokens }} tokens · ${{ r.cost_usd }}{% else %}<i>running...</i>{% endif %}
{% if r.error %}<div class="banner warn">Failed safely, nothing was proposed: {{ r.error }}</div>{% endif %}{% if r.summary %}<p>{{ r.summary }}</p>{% endif %}
<table>{% for t in r.log %}<tr><td><code>{{ t.tool }}</code></td><td><small>{{ t.args[:140] }}</small></td><td><small>{{ 'ok' if t.ok else 'REJECTED' }}: {{ t.result[:170] }}</small></td></tr>{% endfor %}</table></div>
{% else %}<p class="mut">No runs yet.</p>{% endfor %}""", refresh=any(not r["ended_at"] for r in rs), rs=rs)


@app.route("/approvals")
def approvals():
    return page("""{% for p in pend %}<div class="card"><h3>#{{ p.id }} · {{ p.patient_id }} ·
{% if p.action == 'set_priority' %}Priority <span class="pill" style="background:{{ colours[p.payload.colour] }}">{{ p.payload.colour }}</span> (TEWS {{ p.payload.tews }}){% if p.payload.provisional %} <b>PROVISIONAL: vitals missing</b>{% endif %}
{% elif p.action == 'recheck_flag' %}Re-check in {{ p.payload.minutes }} min{% if p.payload.delta is not none %} (score changed by {{ p.payload.delta }}){% endif %}
{% else %}Refer to {{ p.payload.department }}{% endif %}</h3>
<p><b>Why:</b> {{ p.rationale }}</p><table><tr><th>Cited entry</th><th>Evidence</th></tr>{% for e in p.evidence %}<tr><td><code>{{ e.entry_id }}</code></td>
<td>{% if e.quote %}"{{ e.quote }}"{% if e.discriminator %} → {{ e.discriminator }}{% endif %}{% elif e.note %}{{ e.note }}{% else %}{{ e.field }} = {{ e.value }} ({{ e.points }} pts){% endif %}</td></tr>{% endfor %}</table>
<form method="post" action="{{ url_for('decide', pid=p.id) }}" style="margin-top:10px"><input type="hidden" name="_csrf" value="{{ csrf }}">
<select name="clinician" required>{% for c in clin %}<option value="{{ c.name }}">{{ c.name }} ({{ c.role }})</option>{% endfor %}</select>
<input type="password" name="pin" placeholder="PIN" size="6" required>
{% if p.action == 'set_priority' %}<select name="override"><option value="">no override</option>{% for c in ['Green','Yellow','Orange','Red'] %}<option>{{ c }}</option>{% endfor %}</select>{% endif %}
<input name="note" placeholder="note (required for override)" size="30">
<button name="decision" value="approved">Approve</button> <button class="no" name="decision" value="rejected">Reject</button></form></div>
{% else %}<p class="mut">Nothing waiting for approval.</p>{% endfor %}""", pend=gate.pending_view(), clin=gate.clinicians())


@app.route("/approvals/<int:pid>", methods=["POST"])
def decide(pid):
    f = request.form
    try:
        r = gate.decide(pid, f.get("clinician", ""), f.get("pin", ""), f.get("decision", ""), f.get("note", ""),
                        f.get("override") or None)
        flash(f"Proposal {pid} {r['decision']} by {r['by']}.")
    except gate.GateError as e:
        flash(f"Not saved: {e}")
    return redirect(url_for("approvals"))


@app.route("/audit")
def audit():
    ok, msg = gate.verify_chain()
    rows = db.rows("SELECT * FROM approvals ORDER BY id DESC")
    for r in rows:
        r["snap"] = json.loads(r["snapshot"])
    return page("""<div class="banner {{ 'ok' if ok else 'warn' }}">{{ msg }}</div><div class="card"><table><tr><th>#</th><th>When</th><th>Patient · action</th><th>Decision</th><th>Clinician</th><th>Note</th></tr>
{% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.decided_at }}</td><td>{{ r.snap.patient_id }} · {{ r.snap.action }} <small>(run {{ r.snap.run_id }}, {{ r.snap.evidence|length }} cited, {{ r.snap.inputs|length }} inputs snapshotted)</small></td>
<td>{{ r.decision }}{% if r.override_value %} → {{ r.override_value }}{% endif %}</td><td>{{ r.clinician }}</td><td>{{ r.note }}</td></tr>{% endfor %}</table></div>""", rows=rows, ok=ok, msg=msg)


@app.route("/digest", methods=["POST"])
def send_digest():
    from . import emailer
    subj, body, _ = analytics.build_digest(24)
    try:
        how = emailer.send_email(subj, body, attachments={"audit_log_deidentified.csv": analytics.audit_csv()})
        flash(f"Digest sent via Gmail ({how}).")
    except Exception as e:  # noqa: BLE001
        flash(f"E-mail not sent: {e}")
    return redirect(url_for("home"))


def serve(port: int = 8000):
    db.init()
    print(f"Triage Desk running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
