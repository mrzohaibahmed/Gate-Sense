@echo off
rem GateSense: start the app (API + web dashboard, one process) in its own window and open it in the browser.
rem Log in with your GateSense account; camera workers are started from the Live cameras page. Stop with stop.bat.
rem On the first start (no users yet) the first admin account gets GATESENSE_ADMIN_PASSWORD (from .env or the
rem environment) and must change it at the first login.
rem
rem The web dashboard is rebuilt here when web\ changed since the last build (needs Node.js); without Node.js an
rem existing build in web\dist is used.
rem Address: http://localhost:8000 by default. To change it: set GATESENSE_PORT, or GATESENSE_HOST to let other
rem computers in (then HTTPS: GATESENSE_SSL_CERTFILE and GATESENSE_SSL_KEYFILE; see README.md).
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
rem A real environment variable wins over .env. If a variable is missing, the Live cameras page says which one.
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo The Python environment is missing. Set it up first: see "Setup" in README.md.
    pause
    exit /b 1
)

rem 0 = ready, 1 = can't start (the reason is printed), 3 = already running (the browser is opened instead).
"%PY%" -m api.run --preflight
if errorlevel 3 exit /b 0
if errorlevel 1 (
    pause
    exit /b 1
)

start "GateSense" /min "%PY%" -m api.run --open --no-build
echo GateSense is starting. It opens in your browser in a few seconds.
echo It runs in the minimized "GateSense" window; use stop.bat to stop it.
rem Short pause so the message can be read (ping works even without an interactive console).
ping -n 6 127.0.0.1 >nul
