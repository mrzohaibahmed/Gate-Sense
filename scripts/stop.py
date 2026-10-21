"""Stop GateSense (stop.bat runs this): every camera worker cleanly, then the dashboard.

Camera workers get their stop file (data/run/<camera>.stop), whether they
were started from the dashboard or the command line. The dashboard launcher
gets data/run/_dashboard.stop and shuts Streamlit down. Nothing is killed
here: if something doesn't stop within the timeout, its process ID is printed.

    .venv\\Scripts\\python scripts\\stop.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, workers  # noqa: E402

DASHBOARD_TIMEOUT = 30.0


def main() -> int:
    ok = True
    results = workers.WorkerManager().stop_running()
    for camera, result in results:
        print(f"Camera {camera}: {result}")
        ok &= result == "stopped cleanly"
    if not results:
        print("No camera workers running.")

    pid = workers.dashboard_pid()
    if pid is None:
        workers.status_path(workers.DASHBOARD).unlink(missing_ok=True)  # stale info from a crash
        print("Dashboard not running.")
        return 0 if ok else 1
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    workers.stop_path(workers.DASHBOARD).touch()
    deadline = time.monotonic() + DASHBOARD_TIMEOUT
    while time.monotonic() < deadline:
        if not workers.pid_alive(pid):
            print("Dashboard stopped.")
            return 0 if ok else 1
        time.sleep(0.2)
    print(f"Dashboard did not stop within {DASHBOARD_TIMEOUT:.0f} s; close its window (process {pid}).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
