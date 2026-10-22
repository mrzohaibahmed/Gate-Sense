"""FastAPI app factory: `create_app()`; `python -m api.run` serves it on API_HOST:API_PORT.

The lifespan creates or upgrades the database (users, sessions, sites), creates
the first admin if there are no users (api/auth.py), opens the worker manager
(data/run/_api.json) and, on shutdown, stops every camera worker this API
started. Browsers may call the API only from config.API_CORS_ORIGINS (the
Vite dev server), with the session cookie. Every endpoint except login and
the health check needs a login and a permission (api/permissions.py).
"""

import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from api import __version__, auth
from api.db import Database
from api.routes import auth as auth_routes
from api.routes import cameras, users
from api.workers import WorkerManager
from app import config

PUBLIC_PATHS = {"/api/health", "/api/auth/login"}  # the only endpoints without require_permission


class DatabaseStatus(BaseModel):
    status: Literal["ok", "missing", "error"]
    backend: str
    detail: str | None = None


class Health(BaseModel):
    status: Literal["ok", "degraded"]  # degraded: no database yet, or it can't be read (HTTP 503)
    version: str
    started: datetime
    uptime_seconds: float
    database: DatabaseStatus


def create_app(workers: WorkerManager | None = None, database: Database | None = None) -> FastAPI:
    """`workers` and `database` are injectable for tests."""
    manager = workers or WorkerManager()
    database = database or Database()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started = time.time()
        database.connect().close()  # create or upgrade the tables before anything uses them
        if message := auth.bootstrap_admin(database):
            print(message, flush=True)
        manager.open(f"http://{config.API_HOST}:{config.API_PORT}")
        try:
            yield
        finally:
            for camera, result in await run_in_threadpool(manager.close):
                print(f"Camera {camera}: {result}", flush=True)

    app = FastAPI(title="GateSense API", version=__version__, lifespan=lifespan,
                  description="Log in with POST /api/auth/login (it sets the session cookie this page then sends).")
    app.state.workers, app.state.db, app.state.started = manager, database, time.time()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.API_CORS_ORIGINS,
        allow_credentials=True,  # the session cookie
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=Health, tags=["system"])
    def health(response: Response) -> Health:
        """Public (no login), for monitoring: version, uptime and database status. 503 if the database can't be
        read."""
        db = DatabaseStatus(**database.status())
        if db.status == "error":
            response.status_code = 503
        started = app.state.started
        return Health(
            status="ok" if db.status == "ok" else "degraded", version=__version__,
            started=datetime.fromtimestamp(started), uptime_seconds=round(time.time() - started, 1), database=db,
        )

    app.include_router(auth_routes.router)
    app.include_router(users.router)
    app.include_router(cameras.router)
    return app
