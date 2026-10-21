"""Start the GateSense dashboard on localhost (start.bat runs this; stop.bat stops it).

The admin password comes from the GATESENSE_ADMIN_PASSWORD environment
variable, or from GATESENSE_ADMIN_PASSWORD=... in .env in the project root
(copy .env.example). A real environment variable wins over .env.
    .venv\\Scripts\\python scripts\\dashboard.py            # run in this window
    .venv\\Scripts\\python scripts\\dashboard.py --open     # and open the browser when it's ready
    .venv\\Scripts\\python scripts\\dashboard.py --preflight

--preflight only checks: exit 0 = ready to start, 1 = can't start (reason
printed), 3 = already running (the browser is opened instead).

While running, the launcher writes data/run/_dashboard.json and stops cleanly
when data/run/_dashboard.stop appears (scripts/stop.py): it stops every camera
worker first, then the Streamlit server. Host and port come from app/config.py.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, workers  # noqa: E402
from app.env import admin_password_source  # noqa: E402
from app.fileutil import atomic_write_bytes  # noqa: E402

ALREADY_RUNNING = 3


def url() -> str:
    return f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", config.DASHBOARD_PORT)) == 0


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{url()}/_stcore/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def preflight() -> int:
    """Can the dashboard start? Never prints the password, only where it was found."""
    if workers.dashboard_pid():
        print(f"GateSense is already running: {url()}")
        webbrowser.open(url())
        return ALREADY_RUNNING
    source = admin_password_source()  # loads .env; the Streamlit process inherits the result
    if source is None:
        print(
            f"No admin password found. Set {config.DASHBOARD_PASSWORD_ENV} in the environment, "
            f"or put {config.DASHBOARD_PASSWORD_ENV}=... in {config.ENV_PATH.name} (see .env.example)."
        )
        return 1
    print(f"Admin password: found ({source})")
    if port_in_use():
        print(f"Port {config.DASHBOARD_PORT} is already in use by another program. Close it, or change "
              "DASHBOARD_PORT in app/config.py.")
        return 1
    return 0


def stop_workers() -> None:
    for camera, result in workers.WorkerManager().stop_running():
        print(f"Camera {camera}: {result}")


def supervise(proc: subprocess.Popen, open_browser: bool) -> int:
    """Wait for Streamlit to exit, or for stop.bat's stop file; open the browser once it answers."""
    stop_file = workers.stop_path(workers.DASHBOARD)
    opened = not open_browser
    while proc.poll() is None:
        if stop_file.exists():
            print("Stop requested: stopping camera workers, then the dashboard.")
            stop_workers()
            proc.terminate()
            proc.wait(10)
            return 0
        if not opened and healthy():
            webbrowser.open(url())
            opened = True
        time.sleep(0.5)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the GateSense dashboard.")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the browser when it's ready")
    parser.add_argument("--preflight", action="store_true", help="only check whether it can start")
    args = parser.parse_args()

    code = preflight()
    if args.preflight or code:
        return code

    env = dict(os.environ)
    cmd = [
        workers.python_command(sys.executable, env), "-m", "streamlit", "run", str(ROOT / "dashboard" / "main.py"),
        "--server.address", config.DASHBOARD_HOST,
        "--server.port", str(config.DASHBOARD_PORT),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--client.toolbarMode", "minimal",  # no "Deploy" button or developer menu
    ]
    print(f"GateSense dashboard: {url()}  (stop it with stop.bat, or close this window)")
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    stop_file = workers.stop_path(workers.DASHBOARD)
    stop_file.unlink(missing_ok=True)
    info = workers.status_path(workers.DASHBOARD)
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env)
    atomic_write_bytes(info, json.dumps({
        "pid": os.getpid(), "streamlit_pid": proc.pid, "url": url(),
        "started": datetime.now().isoformat(timespec="seconds"),
    }).encode("utf-8"))
    try:
        return supervise(proc, args.open)
    except KeyboardInterrupt:  # Ctrl+C in this window
        stop_workers()
        proc.terminate()
        return 0
    finally:
        info.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
