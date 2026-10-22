"""FastAPI app factory: `create_app()`; `python -m api.run` serves it on API_HOST:API_PORT.

The lifespan opens the worker manager (data/run/_api.json) and, on shutdown,
stops every camera worker this API started. Browsers may call the API only
from config.API_CORS_ORIGINS (the Vite dev server). There is no login yet:
the server binds to 127.0.0.1 only.
"""

import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from api import __version__
from api.db import Database
from api.workers import WorkerManager
from app import config


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
        manager.open(f"http://{config.API_HOST}:{config.API_PORT}")
        try:
            yield
        finally:
            for camera, result in await run_in_threadpool(manager.close):
                print(f"Camera {camera}: {result}")

    app = FastAPI(title="GateSense API", version=__version__, lifespan=lifespan)
    app.state.workers, app.state.db, app.state.started = manager, database, time.time()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.API_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=Health)
    def health(response: Response) -> Health:
        db = DatabaseStatus(**database.status())
        if db.status == "error":
            response.status_code = 503
        started = app.state.started
        return Health(
            status="ok" if db.status == "ok" else "degraded", version=__version__,
            started=datetime.fromtimestamp(started), uptime_seconds=round(time.time() - started, 1), database=db,
        )

    return app
