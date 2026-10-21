@echo off
rem GateSense: start the dashboard in its own window and open it in the browser.
rem Camera workers are then started from the dashboard's Cameras page. Stop everything with stop.bat.
rem
rem Camera passwords are not stored in app/config.py. Camera sources there use placeholders such as
rem   rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101
rem Set the variable in ONE of these ways (special characters like @ and # are fine, no escaping needed):
rem   1. Recommended: in the .env file in this folder (git-ignored), one line:  GATESENSE_CAM_PASS=your-password
rem      (copy .env.example to .env first; changes there are picked up the next time a camera starts)
rem   2. For this window only, before starting:  set "GATESENSE_CAM_PASS=your-password"
rem   3. For your Windows user account, permanently:  setx GATESENSE_CAM_PASS "your-password"
rem      (takes effect in windows opened afterwards; Windows stores it unencrypted in your user profile)
rem      In .env, put a password containing spaces, # or quotes in single quotes:  GATESENSE_CAM_PASS='p@ss #1'
rem A real environment variable wins over .env. If a variable is missing, the Cameras page says which one.
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo The Python environment is missing. Set it up first: see "Setup" in README.md.
    pause
    exit /b 1
)

"%PY%" scripts\dashboard.py --preflight
if errorlevel 3 exit /b 0
if errorlevel 1 (
    pause
    exit /b 1
)

start "GateSense dashboard" /min "%PY%" scripts\dashboard.py --open
echo GateSense is starting. The dashboard opens in your browser in a few seconds.
echo It runs in the minimized "GateSense dashboard" window; use stop.bat to stop it.
rem Short pause so the message can be read (ping works even without an interactive console).
ping -n 6 127.0.0.1 >nul
