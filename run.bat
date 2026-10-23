@echo off
REM One-command local start (Windows). Optional: set GEMINI_API_KEY=... first.
cd /d %~dp0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -q -r requirements-dev.txt
echo Open http://localhost:8080
uvicorn app.main:app --port 8080
