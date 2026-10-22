@echo off
rem GateSense API (FastAPI) on http://127.0.0.1:8000, next to the Streamlit dashboard (start.bat).
rem Runs in this window: press Ctrl+C to stop it; the camera workers it started are stopped first.
rem Health check: http://127.0.0.1:8000/api/health   Interactive docs: http://127.0.0.1:8000/docs
rem Host and port are API_HOST / API_PORT in app\config.py.
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo The Python environment is missing. Set it up first: see "Setup" in README.md.
    pause
    exit /b 1
)

"%PY%" -m api.run
if errorlevel 1 pause
