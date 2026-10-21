@echo off
rem GateSense: stop every camera worker cleanly, then the dashboard.
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo The Python environment is missing: nothing to stop.
    pause
    exit /b 1
)

"%PY%" scripts\stop.py
if errorlevel 1 (
    pause
    exit /b 1
)
rem Short pause so the result can be read (ping works even without an interactive console).
ping -n 4 127.0.0.1 >nul
