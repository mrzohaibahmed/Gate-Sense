"""Start GateSense: the API and the web dashboard, one process on one port. start.bat runs this.

    .venv\\Scripts\\python -m api.run              # run in this window (Ctrl+C stops it)
    .venv\\Scripts\\python -m api.run --open       # and open the browser when it's ready
    .venv\\Scripts\\python -m api.run --preflight  # only check (and build the web dashboard if needed)

Host and port: GATESENSE_HOST / GATESENSE_PORT (default 127.0.0.1:8000). A host other than this PC needs HTTPS
(GATESENSE_SSL_CERTFILE + GATESENSE_SSL_KEYFILE), or GATESENSE_ALLOW_HTTP=1 to run without it anyway (a warning
is printed). --preflight exit codes: 0 = ready, 1 = can't start (reason printed), 3 = already running (the
browser is opened instead).

While running, data/run/_api.json says where; data/run/_api.stop (stop.bat) stops it cleanly: the camera workers
it started first, then the server. Ctrl+C does the same.
"""

import argparse
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import closing

import uvicorn

from api import web
from api.workers import api_pid
from app import config, dbconn, workers
from app.env import admin_password_source

ALREADY_RUNNING = 3
API = "_api"  # data/run/_api.json (written by the running app), _api.stop (asks it to stop)


def url() -> str:
    host = "localhost" if config.API_HOST in ("127.0.0.1", "::1") else config.API_HOST
    return f"{'https' if config.HTTPS else 'http'}://{host}:{config.API_PORT}"


def port_in_use() -> bool:
    host = "127.0.0.1" if config.API_HOST in ("0.0.0.0", "::") else config.API_HOST
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, config.API_PORT)) == 0


def https_problem() -> tuple[str | None, str | None]:
    """(error, warning) about serving without HTTPS on a host other than this PC."""
    if config.API_HOST in config.LOCAL_HOSTS:
        if bool(config.SSL_CERTFILE) != bool(config.SSL_KEYFILE):
            return "Set both GATESENSE_SSL_CERTFILE and GATESENSE_SSL_KEYFILE (or neither).", None
        return None, None
    if config.HTTPS:
        return None, None
    message = (f"GATESENSE_HOST is {config.API_HOST}: other computers can reach GateSense, so passwords and the "
               "session cookie would cross the network unencrypted.")
    if config.ALLOW_HTTP:
        return None, f"WARNING: {message} Running without HTTPS because GATESENSE_ALLOW_HTTP=1."
    return (f"{message} Set GATESENSE_SSL_CERTFILE and GATESENSE_SSL_KEYFILE (PEM files) to use HTTPS, or "
            "GATESENSE_ALLOW_HTTP=1 to run without it anyway."), None


def needs_admin_password() -> bool:
    """No users yet (first start): the first admin is created from GATESENSE_ADMIN_PASSWORD."""
    if url := dbconn.server_url():
        from app import mssql

        try:
            with closing(mssql.Connection(url)) as conn:
                if mssql.schema_version(conn) is None:
                    return True  # an empty database: the tables are created at the start
                return conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        except dbconn.ERRORS:
            return True
    if not config.DB_PATH.is_file():
        return True
    try:
        with sqlite3.connect(f"{config.DB_PATH.resolve().as_uri()}?mode=ro", uri=True) as conn:
            return conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    except sqlite3.Error:  # no users table yet: an older database
        return True


def preflight(open_browser: bool = True, build: bool = True) -> int:
    if pid := api_pid():
        print(f"GateSense is already running (process {pid}): {url()}")
        if open_browser:
            webbrowser.open(url())
        return ALREADY_RUNNING
    from api.main import startup_problems

    if problems := startup_problems():
        for problem in problems:
            print(f"ERROR: {problem}")
        return 1
    error, warning = https_problem()
    if error:
        print(f"ERROR: {error}")
        return 1
    if warning:
        print(warning)
    if needs_admin_password() and admin_password_source() is None:
        print(f"No users yet: set {config.DASHBOARD_PASSWORD_ENV} in the environment or in .env (see .env.example) "
              "for the first admin account.")
        return 1
    if port_in_use():
        print(f"Port {config.API_PORT} is already in use by another program. Close it, or set GATESENSE_PORT.")
        return 1
    status = web.build_status()
    if status != "ok" and build:
        print("Building the web dashboard (it changed since the last build)..." if status == "stale"
              else "Building the web dashboard (first start)...")
        if not web.build():
            if status == "missing":
                print("ERROR: the web dashboard isn't built. Install Node.js (https://nodejs.org), then start again.")
                return 1
            print("WARNING: using the previous build of the web dashboard.")
    return 0


def watch(server: uvicorn.Server, open_browser: bool) -> None:
    """Stop on data/run/_api.stop; open the browser once the app answers."""
    stop = workers.stop_path(API)
    opened = not open_browser
    while not server.should_exit:
        if stop.exists():
            print("Stop requested: stopping the camera workers, then GateSense.", flush=True)
            stop.unlink(missing_ok=True)
            server.should_exit = True
            return
        if not opened and server.started:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{config.API_PORT}/api/health", timeout=2):
                    pass
            except OSError:
                pass  # HTTPS or not quite ready: open anyway once started
            webbrowser.open(url())
            opened = True
        time.sleep(0.5)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.run", description=__doc__.splitlines()[0])
    parser.add_argument("--open", action="store_true", help="open the browser when GateSense is ready")
    parser.add_argument("--preflight", action="store_true", help="only check whether it can start")
    parser.add_argument("--no-build", action="store_true", help="don't rebuild the web dashboard")
    args = parser.parse_args(argv)
    if args.preflight:
        return preflight(open_browser=True, build=not args.no_build)
    code = preflight(open_browser=args.open, build=not args.no_build)
    if code:
        return code
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    workers.stop_path(API).unlink(missing_ok=True)  # a stale request from an earlier run
    print(f"GateSense: {url()}  (API docs: {url()}/docs). Close with stop.bat or Ctrl+C.", flush=True)
    server = uvicorn.Server(uvicorn.Config(
        "api.main:create_app", factory=True, host=config.API_HOST, port=config.API_PORT, log_level="info",
        ssl_certfile=config.SSL_CERTFILE if config.HTTPS else None,
        ssl_keyfile=config.SSL_KEYFILE if config.HTTPS else None))
    threading.Thread(target=watch, args=(server, args.open), name="gatesense-watch", daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
