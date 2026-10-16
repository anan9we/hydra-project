#!/usr/bin/env bash
# One command: ./start.sh   (creates venv, installs, sets up synthetic demo data, starts the web app)
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
[ -d venv ] || python3 -m venv venv
./venv/bin/python -m pip install -q -r requirements.txt
[ -f triage.db ] || ./venv/bin/python -m triage_desk setup
if command -v ollama >/dev/null 2>&1; then
  MODEL=$(grep -E '^TRIAGE_MODEL=' .env | cut -d= -f2); MODEL=${MODEL:-qwen2.5:7b}
  ollama list | grep -q "${MODEL%%:*}" || ollama pull "$MODEL"
else
  echo "[NOTE] Ollama not found. Install it from https://ollama.com, then: ollama pull qwen2.5:7b"
fi
echo "Open http://127.0.0.1:8000   (demo clinician PINs: 1234 / 5678, synthetic data only)"
./venv/bin/python -m triage_desk web
