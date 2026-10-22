"""System information and "backup now" (Settings > System, admin only)."""

import sys
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import __version__, auth, maintenance
from api.auth import CurrentUser, require_permission
from app import config, enrollment

router = APIRouter(prefix="/api/system", tags=["system"], responses=auth.PROTECTED_RESPONSES)
manage = require_permission("settings:manage")


class Backup(BaseModel):
    file: str
    size_bytes: int
    created: datetime


class Maintenance(BaseModel):
    last_run: datetime | None = Field(description="the last nightly job (backup, old backups, old snapshots)")
    last_ok: bool | None
    last_error: str | None
    backup_hour: int = Field(description="the nightly job runs once a day at or after this hour")
    backup_retention_days: int = Field(description="nightly backups are kept this long")
    snapshot_retention_days: int = Field(description="event snapshots are kept this long (setting)")
    snapshots_bytes: int


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
    maintenance: Maintenance


def backups() -> list[Backup]:
    folder = maintenance.backup_dir()
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
    config.refresh_settings()
    last = maintenance.read_state().get("last") or {}
    return SystemInfo(
        version=__version__, started=datetime.fromtimestamp(started), uptime_seconds=round(time.time() - started, 1),
        python=sys.version.split()[0], database_bytes=size(config.DB_PATH) + size(wal), database_schema=version,
        events=events, people=people, gallery_bytes=size(config.GALLERY_PATH),
        enrolled=len(enrollment.load_gallery().counts()), backups=backups(),
        cameras_running=sum(w.state != "stopped" for w in request.app.state.workers.statuses()),
        maintenance=Maintenance(
            last_run=datetime.fromisoformat(last["time"]) if last.get("time") else None, last_ok=last.get("ok"),
            last_error=last.get("error"), backup_hour=config.BACKUP_HOUR,
            backup_retention_days=config.BACKUP_RETENTION_DAYS,
            snapshot_retention_days=config.SNAPSHOT_RETENTION_DAYS, snapshots_bytes=maintenance.snapshot_bytes()))


class BackupIn(BaseModel):
    reason: str = "backup from the dashboard"


@router.post("/backup", response_model=Backup, status_code=201)
async def backup_now(body: BackupIn, request: Request, admin: CurrentUser = Depends(manage)) -> Backup:
    """Copy the database (consistent, while cameras keep writing: SQLite's online backup) and the face gallery to
    data/backups/. Audited. Manual copies are never deleted automatically (nightly ones after BACKUP_RETENTION_DAYS)."""

    def run() -> Backup:
        dest = maintenance.backup_database(auth.db_of(request), maintenance.MANUAL)
        return Backup(file=dest.name, size_bytes=dest.stat().st_size, created=datetime.now())

    result = await run_in_threadpool(run)
    await run_in_threadpool(auth.db_of(request).log_change, admin.username, "backup", "database",
                            {"file": result.file, "bytes": result.size_bytes}, body.reason)
    return result
