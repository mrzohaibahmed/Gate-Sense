"""System information and "backup now" (Settings > System, admin only)."""

import shutil
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import __version__, auth
from api.auth import CurrentUser, require_permission
from app import config, enrollment

router = APIRouter(prefix="/api/system", tags=["system"], responses=auth.PROTECTED_RESPONSES)
manage = require_permission("settings:manage")


class Backup(BaseModel):
    file: str
    size_bytes: int
    created: datetime


class SystemInfo(BaseModel):
    version: str
    started: datetime
    uptime_seconds: float
    python: str
    database_bytes: int = Field(description="the database file plus its write-ahead log")
    database_schema: int = Field(description="schema version (PRAGMA user_version)")
    events: int
    people: int
    gallery_bytes: int
    enrolled: int = Field(description="people with face embeddings")
    backups: list[Backup] = Field(description="newest first: data/backups/ (copies made before upgrades, and manual)")
    cameras_running: int


def backup_dir():
    return config.DB_PATH.parent / "backups"


def backups() -> list[Backup]:
    folder = backup_dir()
    files = sorted(folder.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    return [Backup(file=p.name, size_bytes=p.stat().st_size, created=datetime.fromtimestamp(p.stat().st_mtime))
            for p in files]


def size(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@router.get("", response_model=SystemInfo)
def system_info(request: Request, _: CurrentUser = Depends(manage)) -> SystemInfo:
    db = auth.db_of(request)
    with db.conn() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        events = conn.execute("SELECT count(*) FROM attendance_events").fetchone()[0]
        people = conn.execute("SELECT count(*) FROM persons").fetchone()[0]
    wal = config.DB_PATH.with_name(config.DB_PATH.name + "-wal")
    started = request.app.state.started
    return SystemInfo(
        version=__version__, started=datetime.fromtimestamp(started), uptime_seconds=round(time.time() - started, 1),
        python=sys.version.split()[0], database_bytes=size(config.DB_PATH) + size(wal), database_schema=version,
        events=events, people=people, gallery_bytes=size(config.GALLERY_PATH),
        enrolled=len(enrollment.load_gallery().counts()), backups=backups(),
        cameras_running=sum(w.state != "stopped" for w in request.app.state.workers.statuses()))


class BackupIn(BaseModel):
    reason: str = "backup from the dashboard"


@router.post("/backup", response_model=Backup, status_code=201)
async def backup_now(body: BackupIn, request: Request, admin: CurrentUser = Depends(manage)) -> Backup:
    """Copy the database (consistent, while cameras keep writing: SQLite's online backup) and the face gallery to
    data/backups/. Audited."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = backup_dir()

    def run() -> Backup:
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{config.DB_PATH.stem}-manual-{stamp}.db"
        with closing(auth.db_of(request).connect()) as src, closing(sqlite3.connect(dest)) as out:
            src.backup(out)
        if config.GALLERY_PATH.is_file():
            shutil.copy2(config.GALLERY_PATH, folder / f"{config.GALLERY_PATH.stem}-manual-{stamp}.npz")
        return Backup(file=dest.name, size_bytes=dest.stat().st_size, created=datetime.now())

    result = await run_in_threadpool(run)
    await run_in_threadpool(auth.db_of(request).log_change, admin.username, "backup", "database",
                            {"file": result.file, "bytes": result.size_bytes}, body.reason)
    return result
