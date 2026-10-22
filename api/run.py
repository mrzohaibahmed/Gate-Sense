"""Start the GateSense API: `python -m api.run` (start-api.bat runs this).

Serves api.main:create_app with uvicorn on API_HOST:API_PORT from app/config.py
(127.0.0.1:8000). Ctrl+C stops it cleanly: the camera workers it started are
stopped first. Exit codes before serving: 1 = can't start (port taken),
3 = already running. After Ctrl+C / Ctrl+Break the code comes from uvicorn
(it re-raises the signal), so it isn't a reliable success signal.
"""

import socket
import sys

import uvicorn

from api.workers import api_pid
from app import config

ALREADY_RUNNING = 3


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((config.API_HOST, config.API_PORT)) == 0


def main() -> int:
    url = f"http://{config.API_HOST}:{config.API_PORT}"
    if pid := api_pid():
        print(f"The GateSense API is already running (process {pid}): {url}")
        return ALREADY_RUNNING
    if port_in_use():
        print(f"Port {config.API_PORT} is already in use by another program. Close it, or change API_PORT in "
              "app/config.py.")
        return 1
    print(f"GateSense API: {url}  (health: {url}/api/health, docs: {url}/docs). Press Ctrl+C to stop.", flush=True)
    uvicorn.run("api.main:create_app", factory=True, host=config.API_HOST, port=config.API_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
