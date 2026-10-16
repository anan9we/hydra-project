# Triage Desk

An agent that sits beside the triage nurse in a hospital outpatient department. It reads each intake record,
**proposes** a priority (South African Triage Scale), a re-check, or a referral, and **cites the nurse's own entry
behind every claim**. A named clinician approves or rejects everything. It never diagnoses, never prescribes, and
never changes the queue on its own.

> Prototype for the African Agentic AI Design Challenge (Health track, *The Triage Desk*). **Synthetic patients only.**
> Not a medical device. The SATS discriminator list and target times must be verified by a clinician before any real use.

![architecture](docs/architecture.png)

## Run it (one command)

Prerequisites: Python 3.10+ and [Ollama](https://ollama.com) (the open-weights model runs on your machine).

| OS | Command |
|---|---|
| Windows | double-click `start.bat` |
| macOS / Linux | `./start.sh` |

It creates a virtual environment, installs dependencies, creates a database with 6 **synthetic** patients and 2 demo
clinicians, pulls `qwen2.5:7b` through Ollama, and starts the web app at <http://127.0.0.1:8000>.
Demo clinician PINs: `1234` (Dr. Wanjiru Kamau) and `5678` (Nurse Otieno). These are demo values for synthetic data.

### See the agent work (what to screen-record)

```
python -m triage_desk triage P-001 P-003 P-004 P-005     # tool calls print live
python -m triage_desk pending                            # proposals + the entries they cite
python -m triage_desk approve 1 --clinician "Dr. Wanjiru Kamau" --pin 1234
python -m triage_desk queue                              # the live queue exists only after approval
python -m triage_desk audit                              # hash-chained decision log
```

Or use the web app: Queue > *Run triage agent* > Agent runs (live trace) > Approvals.

## How it works

1. **Intake**: nurse records vitals and a note (English / Kiswahili / Sheng).
2. **Agent** (Pydantic AI + open-weights model) calls six tools on our **MCP server**:
   `get_intake`, `score_tews`, `assert_discriminator`, `submit_priority`, `flag_recheck`, `route_referral`.
3. The **server** computes the SATS score in code and derives the colour: `max(TEWS colour, verified discriminators)`.
   The model never types a colour or a number. Discriminators need a **verbatim quote** from the nurse's note.
4. Missing vitals produce a **re-check flag**, not a guess. Children (< 12) are **escalated, not scored**.
5. Everything the agent wants to do is saved as a **pending proposal**. A clinician approves with name + PIN in the
   web app. Only then does the live queue change, and the decision is written to a hash-chained audit log.

Details, failure handling and the data-flow diagram: [ARCHITECTURE.md](ARCHITECTURE.md).
Tests and honest results: [EVALS.md](EVALS.md). Hackathon text: [SUBMISSION.md](SUBMISSION.md).

## Data stays in the country

- The model is open-weights and runs locally through Ollama (`TRIAGE_PROVIDER=ollama`, the default).
- `TRIAGE_PROVIDER=gemini` exists only to compare a frontier model **on synthetic patients**. A residency guard
  refuses to send any non-synthetic patient to a non-local endpoint.
- E-mail goes through Gmail (servers abroad), so alerts contain **only anonymous ticket ids, counts and colours**:
  no names, vitals or complaint text.

## Gmail setup (about 5 minutes, optional)

1. [Google Cloud Console](https://console.cloud.google.com): create a project and enable the **Gmail API**.
2. **OAuth consent screen**: External; add yourself as a **test user**.
3. **Credentials > Create credentials > OAuth client ID > Desktop app**. Download the JSON and save it as
   `credentials.json` in this folder.
4. In `.env` set `ALERT_EMAIL_TO=you@example.com`.
5. Run `python -m triage_desk gmail-auth` (a browser opens; approve *send email on your behalf*).
   Google shows an "unverified app" warning for test apps: *Advanced > continue*.
6. Check it: `python -m triage_desk email-test`.

Only the `gmail.send` scope is requested (it cannot read your mailbox). `credentials.json` and `token.json` are
git-ignored. While the OAuth app is in *Testing*, Google expires the login after 7 days: re-run step 5 before a demo.
Fallback without OAuth: set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in `.env`.
Alerts are sent after each agent run; `python -m triage_desk digest --send` e-mails a de-identified shift digest.

## Configuration (`.env`)

See `.env.example`. Change the model with `TRIAGE_MODEL` (any Ollama model with tool calling: Qwen, Gemma, Llama,
Mistral, Aya...). Never commit `.env`.

## Tests and evals

```
python -m pytest                                         # 23 tests (no model, no network needed)
python -m triage_desk.evals --layer scoring              # 18 labelled cases + 9 guards, no model
python -m triage_desk.evals --layer agent --repeat 3 --write-report   # needs Ollama running
```

## Project layout

```
triage_desk/sats.py        SATS scoring + discriminators (pure code)
triage_desk/tools.py       the six tools and their guards
triage_desk/mcp_server.py  MCP server (stdio)
triage_desk/agent.py       Pydantic AI agent + output validator
triage_desk/gate.py        clinician approval gate + hash-chained audit log (not reachable by the agent)
triage_desk/runner.py      runs an agent, records tokens / cost / time, fails safe
triage_desk/emailer.py     Gmail OAuth2 + SMTP fallback, de-identified alerts
triage_desk/web.py, cli.py interfaces        evals/ labelled cases       tests/ pytest suite
```

## Licence

MIT. See [LICENSE](LICENSE). Libraries: Pydantic AI, MCP Python SDK, Flask (all open source).
