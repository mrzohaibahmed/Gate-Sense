"""Start the GateSense dashboard on localhost.

The admin password comes from the GATESENSE_ADMIN_PASSWORD environment
variable, or from GATESENSE_ADMIN_PASSWORD=... in .env in the project root
(copy .env.example). A real environment variable wins over .env.
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
from app.env import admin_password_source  # noqa: E402


def main() -> int:
    source = admin_password_source()  # loads .env; the child process inherits the result
    if source is None:
        print(
            f"No admin password found. Set {config.DASHBOARD_PASSWORD_ENV} in the environment, "
            f"or put {config.DASHBOARD_PASSWORD_ENV}=... in {config.ENV_PATH.name} (see .env.example)."
        )
        return 1
    print(f"Admin password: found ({source})")
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
