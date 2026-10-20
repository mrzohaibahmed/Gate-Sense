"""Start the GateSense dashboard on localhost.

The admin password must be in the GATESENSE_ADMIN_PASSWORD environment variable:
    set GATESENSE_ADMIN_PASSWORD=<password>          (cmd)
    $env:GATESENSE_ADMIN_PASSWORD = "<password>"     (PowerShell)
    .venv\\Scripts\\python scripts\\dashboard.py

Host and port come from app/config.py (DASHBOARD_HOST, DASHBOARD_PORT).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402


def main() -> int:
    if not os.environ.get(config.DASHBOARD_PASSWORD_ENV):
        print(f"Set the {config.DASHBOARD_PASSWORD_ENV} environment variable to the admin password first.")
        return 1
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard" / "main.py"),
        "--server.address", config.DASHBOARD_HOST,
        "--server.port", str(config.DASHBOARD_PORT),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    print(f"GateSense dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    try:
        return subprocess.call(cmd, cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
