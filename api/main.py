"""FastAPI app factory: `create_app()`; `python -m api.run` serves it on API_HOST:API_PORT.

The lifespan creates or upgrades the database (users, sessions, sites), creates
the first admin if there are no users (api/auth.py), opens the worker manager
(data/run/_api.json), starts the supervisor that keeps enabled cameras
recording (api/supervisor.py) and, on shutdown, stops every camera worker this
API started. It refuses to start on an unreadable cameras.json or a damaged
database (startup_problems). Browsers may call the API only from config.API_CORS_ORIGINS (the
Vite dev server), with the session cookie. Every endpoint except login and
the health check needs a login and a permission (api/permissions.py).

GateSense runs as one app: the built web dashboard (web/dist) is served at /
(api/web.py), with security headers and a request size limit on everything
(api/security.py), and the nightly backup / snapshot cleanup runs inside the
lifespan (api/maintenance.py).
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from functools import cache
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import __version__, auth, maintenance, security, web
from api import health as health_checks
from api.db import Database
from api.enroll_camera import EnrollCameras
from api.live import EventHub, FrameHub
from api.supervisor import Supervisor
from api.routes import (
    attendance, audit, cameras, enrollment, live, people, people_import, reports, settings, sites, system, users,
)
from api.routes import auth as auth_routes
from api.workers import WorkerManager
from app import config
from app import db as app_db
from app import liveness as app_liveness

PUBLIC_PATHS = {"/api/health", "/api/auth/login"}  # the only endpoints without require_permission


class DatabaseStatus(BaseModel):
    status: Literal["ok", "missing", "error"]
    backend: str
    detail: str | None = None


class Health(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="degraded: see problems (HTTP 503 only when the database "
                                                          "can't be read)")
    version: str
    started: datetime
    uptime_seconds: float
    database: DatabaseStatus
    cameras: list[health_checks.CameraHealth]
    disk: health_checks.DiskHealth
    backup: health_checks.BackupHealth
    problems: list[str] = Field(description="what someone should look at, in plain words; empty = all well")


# Requests that come several times a second while a page is open: live video, the 2 fps snapshot tiles, face
# snapshots in lists. Logging each one would fill the service log; their errors (status 400 and up) are still logged.
QUIET_PATHS = re.compile(r"^/api/(cameras/\d+/(stream|snapshot\.jpg)|enrollment/sessions/[^/]+/stream|snapshots/\d+)"
                         r"(\?|$)")


class QuietAccessLog(logging.Filter):
    """Drops uvicorn's access-log line for a successful request to one of QUIET_PATHS."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            path, status = str(args[2]), args[4]
            if QUIET_PATHS.match(path) and isinstance(status, int) and status < 400:
                return False
        return True


def quiet_access_log() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietAccessLog) for f in logger.filters):
        logger.addFilter(QuietAccessLog())


def startup_problems(database: Database | None = None) -> list[str]:
    """Why GateSense must not start: an unreadable cameras.json (it would start the wrong cameras) or a damaged
    database (PRAGMA quick_check). Checked by start.bat's preflight and again when the app starts."""
    problems = []
    try:
        config.check_cameras()
    except config.CamerasFileError as e:
        problems.append(str(e))
    try:
        path = (database or Database()).path
    except Exception:  # noqa: BLE001  not SQLite: Database.connect reports that itself
        path = None
    if path is not None and (damage := app_db.quick_check(path)):
        problems.append(f"the database {path} is damaged ({damage}). Restore a backup: "
                        ".venv\\Scripts\\python scripts\\restore.py --list")
    return problems


@cache
def face_models():
    """Detector and recognizer for re-enrollment, loaded on first use (a few seconds)."""
    from app.detector import Detector
    from app.recognizer import Recognizer

    return Detector(), Recognizer()


def create_app(workers: WorkerManager | None = None, database: Database | None = None, models=None,
               enroll_opener=None, liveness=None) -> FastAPI:
    """`workers`, `database`, `models` (a callable returning (detector, recognizer)), `enroll_opener` (opens an
    IP camera for the enrollment wizard: (source, name) -> newest-frame reader) and `liveness` (a callable returning
    the enrollment wizard's liveness checker, app.liveness.shared) are injectable for tests."""
    manager = workers or WorkerManager()
    database = database or Database()
    quiet_access_log()
    enroll_cameras = EnrollCameras(manager, enroll_opener)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started = time.time()
        if problems := startup_problems(database):
            raise RuntimeError("GateSense can't start: " + " ".join(problems))
        database.connect().close()  # create or upgrade the tables before anything uses them
        if message := auth.bootstrap_admin(database):
            print(message, flush=True)
        manager.open(f"http://{config.API_HOST}:{config.API_PORT}")
        live_updates = asyncio.create_task(app.state.events.run())
        nightly = asyncio.create_task(maintenance.run_loop(database))
        supervision = asyncio.create_task(app.state.supervisor.run())  # starts the enabled cameras
        try:
            yield
        finally:
            supervision.cancel()  # first: nothing may restart the cameras being stopped below
            await run_in_threadpool(app.state.supervisor.close)
            live_updates.cancel()
            nightly.cancel()
            await run_in_threadpool(enroll_cameras.close_all)
            for camera, result in await run_in_threadpool(manager.close):
                print(f"Camera {camera}: {result}", flush=True)

    app = FastAPI(title="GateSense API", version=__version__, lifespan=lifespan,
                  description="Log in with POST /api/auth/login (it sets the session cookie this page then sends).",
                  generate_unique_id_function=lambda route: route.name)  # operation ids = function names
    app.state.workers, app.state.db, app.state.started = manager, database, time.time()
    app.state.face_models = models or face_models
    app.state.liveness = liveness or app_liveness.shared
    app.state.enroll_cameras = enroll_cameras
    app.state.frames = FrameHub(lambda: manager.live_dir)
    app.state.events = EventHub(database, manager)
    app.state.supervisor = Supervisor(manager, database)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.API_CORS_ORIGINS,
        allow_credentials=True,  # the session cookie
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=Health, tags=["system"])
    def health(response: Response) -> Health:
        """Public (no login), for monitoring: version, uptime, database, each camera (state, Auto, age of its newest
        frame), free disk space, the last nightly backup, and `problems` (plain words; status "degraded" when there
        are any). HTTP 503 only if the database can't be read."""
        db = DatabaseStatus(**database.status())
        if db.status == "error":
            response.status_code = 503
        started = app.state.started
        now, uptime = datetime.now(), round(time.time() - started, 1)
        cams = health_checks.camera_health(manager, app.state.supervisor, now)
        disk = health_checks.disk_health()
        backup = health_checks.backup_health(uptime, now)
        problems = [] if db.status == "ok" else [f"database: {db.detail or db.status}"]
        problems += [c.problem for c in cams if c.problem] + [x.problem for x in (disk, backup) if x.problem]
        return Health(
            status="degraded" if problems else "ok", version=__version__, started=datetime.fromtimestamp(started),
            uptime_seconds=uptime, database=db, cameras=cams, disk=disk, backup=backup, problems=problems,
        )

    @app.exception_handler(config.CamerasFileError)
    async def cameras_file_error(request, exc: config.CamerasFileError) -> JSONResponse:
        """cameras.json became unreadable while running: say so instead of guessing (never the defaults)."""
        return JSONResponse({"detail": str(exc)}, status_code=503)

    # people_import before people: /api/people/import/... must not be taken for /api/people/{person_id}
    for module in (auth_routes, users, attendance, people_import, people, enrollment, live, cameras, sites, audit,
                   settings, system, reports):
        app.include_router(module.router)
    web.mount(app)  # last: its catch-all route sends every other path to the dashboard
    # Outermost last: the size limit and the headers apply to everything, CORS answers included.
    app.add_middleware(security.BodySizeLimit)
    app.add_middleware(security.SecurityHeaders)
    return app
