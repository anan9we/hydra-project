# Submission pack: Triage Desk

## Short description (~300 words, paste into the form)

Triage Desk is an agent that sits beside the triage nurse in a Kenyan county hospital outpatient department, where one or two nurses take vitals and the queue is still ordered by arrival. The problem it attacks: a patient with silent danger signs, such as a fast pulse, low blood pressure or chest pain, waits behind routine cases because nobody has scored the numbers yet.

For each intake record, an open-weights model (Qwen through Ollama, running on the hospital's own machine) plans and calls six tools on a Model Context Protocol server we built. It reads the nurse's vitals and note in English or Kiswahili, computes the South African Triage Scale score in code, and asserts clinical discriminators only by quoting the nurse's own words, which the server verifies. The priority colour is derived by the server, never typed by the model, so every level cites the entry behind it. If vitals are missing it flags a re-check instead of guessing, it flags a patient who worsens between rounds, and children are escalated to paediatrics rather than scored with the adult scale.

The agent only proposes. Every priority, re-check or referral waits for a named clinician who approves with a PIN, and each decision is written to a hash-chained audit log with the inputs. The agent has no path to the live queue. Patient data never leaves the country: the model is local, a residency guard blocks real patients from cloud models, and Gmail alerts to the charge nurse carry only anonymous ticket ids and colours.

Sub-theme: symptom-based priority scoring, with re-check flags. Facility and workflow: the triage point of a county hospital OPD, from vitals taken to the clinician deciding who is seen first. Stack: Pydantic AI, MCP, Ollama, Qwen, Flask, SQLite, Gmail API. Evidence: 18 labelled synthetic cases, with results and known failures reported plainly in EVALS.md.

## Requirement checklist

| Requirement | Where |
|---|---|
| Agent plans, calls tools, recovers, acts | `agent.py`, `tools.py`; recovery tests in `tests/test_agent_wiring.py` |
| One MCP server | `triage_desk/mcp_server.py` (built); see ARCHITECTURE.md |
| Open-source orchestration | Pydantic AI |
| Open-weights model does a full task | Qwen via Ollama (`TRIAGE_PROVIDER=ollama`); patient data local |
| Human in the loop with named clinician, logged | `gate.py`, Approvals page, `audit` |
| At least 3 tools | 6 tools |
| Public repo, OS-approved licence, one-command README | MIT; `start.bat` / `start.sh` |
| Demo video under 3 min | **you record it** (script below) |
| Text description | above |
| ARCHITECTURE.md | included |
| EVALS (>= 10 labelled cases, pass/fail, what is not fixed) | `EVALS.md`, 18 cases. **Run layer 2 on your machine first** |
| Work built during the submission period | confirm with your own timeline |

## Before you submit (things only you can do)

1. `python -m triage_desk.evals --layer agent --repeat 3 --write-report` with Ollama running, then commit `EVALS.md`.
2. Record the demo video (unedited, under 3 minutes, tool calls on screen, no slides). Suggested run:
   - 0:00 `python -m triage_desk triage P-001` shows the tool calls live (intake, score, quote verified, proposal).
   - 0:50 `P-003` (missing vitals) shows a re-check flag instead of a guess; `P-004` (child) shows escalation.
   - 1:30 open the Approvals page: cited evidence, wrong PIN refused, then approve with the right PIN.
   - 2:10 Queue shows the approved patient; Audit page shows the intact chain.
   - 2:40 mention that real patients are blocked from cloud models. Stop.
3. Push to a **public** GitHub repo. Check `.env`, `credentials.json`, `token.json` are NOT in it (they are git-ignored).
4. Replace the generic facility wording with your real facility if you have one.
5. Rotate any API keys, tokens or passwords you ever pasted into a chat or uploaded (Gemini, Telegram, UltraMsg,
   ElevenLabs, Gmail app password).
