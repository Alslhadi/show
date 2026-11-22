#!/usr/bin/env bash
# Mac / Linux (simulation mode only — MT5 needs Windows)
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt; }
. .venv/bin/activate
[ -f .env ] || cp .env.example .env
echo "Dashboard: http://localhost:8000"
while true; do python -m uvicorn app.main:app --host 0.0.0.0 --port 8000; sleep 5; done
