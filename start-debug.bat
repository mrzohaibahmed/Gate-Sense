@echo off
rem GateSense for troubleshooting and development: the same app as start.bat (API and web dashboard), but in this
rem window with its log scrolling, without start.bat's checks, web rebuild or browser. Use start.bat day to day.
rem Press Ctrl+C to stop it; the camera workers it started are stopped first.
rem Health check: http://127.0.0.1:8000/api/health   Interactive docs: http://127.0.0.1:8000/docs
rem Address: http://127.0.0.1:8000 by default; set GATESENSE_HOST and GATESENSE_PORT (environment or .env) to
rem change it.
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
