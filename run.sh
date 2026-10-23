#!/usr/bin/env bash
# One-command local start (Linux/macOS). Optional: export GEMINI_API_KEY=... first.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements-dev.txt
[ -f .env ] && set -a && . ./.env && set +a
echo "Open http://localhost:8080"
uvicorn app.main:app --port 8080
