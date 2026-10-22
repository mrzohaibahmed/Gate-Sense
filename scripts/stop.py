"""Stop GateSense (stop.bat runs this): every camera worker cleanly, then the app.

Camera workers get their stop file (data/run/<camera>.stop), whoever started them. The app (python -m api.run)
gets data/run/_api.stop; a Streamlit dashboard that is still running gets data/run/_dashboard.stop. Nothing is
killed here: if something doesn't stop within the timeout, its process ID is printed.

    .venv\\Scripts\\python scripts\\stop.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.workers import api_pid  # noqa: E402
from app import config, workers  # noqa: E402

TIMEOUT = 30.0


def stop_process(label: str, pid: int | None, name: str) -> bool:
    """Ask a process (by its stop file) to stop and wait for it; True if it isn't running afterwards."""
    if pid is None:
        workers.status_path(name).unlink(missing_ok=True)  # stale info from a crash
        print(f"{label} not running.")
        return True
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    workers.stop_path(name).touch()
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if not workers.pid_alive(pid):
            print(f"{label} stopped.")
            return True
        time.sleep(0.2)
    print(f"{label} did not stop within {TIMEOUT:.0f} s; close its window (process {pid}).")
    return False


def main() -> int:
    ok = True
    results = workers.WorkerManager().stop_running()
    for camera, result in results:
        print(f"Camera {camera}: {result}")
        ok &= result == "stopped cleanly"
    if not results:
        print("No camera workers running.")
    ok &= stop_process("GateSense", api_pid(), "_api")
    ok &= stop_process("Dashboard", workers.dashboard_pid(), workers.DASHBOARD)  # the Streamlit dashboard
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
