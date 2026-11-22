@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
  call .venv\Scripts\activate
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate
)
if not exist .env copy .env.example .env
echo.
echo   Dashboard: http://localhost:8000
echo.
:loop
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
echo Restarting in 5 seconds...
timeout /t 5 >nul
goto loop
