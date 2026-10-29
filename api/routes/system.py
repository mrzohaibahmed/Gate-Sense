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
    files: list[str] = Field([], description="every file of the set: database, gallery, known/ photos (zip), "
                                             "cameras.json, settings.json")
    copied_to: str | None = Field(None, description="the second backup folder the set was copied to")
    copy_error: str | None = Field(None, description="why the copy to the second folder failed (the set in "
                                                     "data/backups/ is fine)")


class Maintenance(BaseModel):
    last_run: datetime | None = Field(description="the last nightly job (backup, old backups, old snapshots)")
    last_ok: bool | None
    last_error: str | None
    backup_hour: int = Field(description="the nightly job runs once a day at or after this hour")
    backup_keep: int = Field(description="nightly backup sets kept in each backup folder")
    backup_copy_dir: str | None = Field(description="second backup folder (GATESENSE_BACKUP_COPY_DIR), if set")
    snapshot_retention_days: int = Field(description="event snapshots are kept this long (setting)")
    snapshots_bytes: int


class DeviceInfo(BaseModel):
    active: str = Field(description="where the face and liveness models run: cuda or cpu")
    label: str = Field(description='"NVIDIA GPU (CUDA)" or "CPU"')
    requested: str = Field(description="the GATESENSE_DEVICE setting: auto, cuda or cpu")
    providers: list[str] = Field(description="onnxruntime execution providers, in order of preference")
    warning: str | None = Field(None, description="why it runs on the CPU although the setting allowed the GPU")


class CameraDevice(BaseModel):
    camera: str
    label: str = Field(description="where this running camera's models run: NVIDIA GPU (CUDA) or CPU")
    warning: str | None = None


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
    device: DeviceInfo = Field(description="this app's device (the enrollment models); GATESENSE_DEVICE is read at "
                                           "start, so a change needs a restart")
    camera_devices: list[CameraDevice] = Field(description="the device each running camera reported at its start")
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
    from app import models  # here, not at import: it loads onnxruntime and InsightFace

    dev = models.device()
    workers = request.app.state.workers.statuses()
    return SystemInfo(
        version=__version__, started=datetime.fromtimestamp(started), uptime_seconds=round(time.time() - started, 1),
        python=sys.version.split()[0], database_bytes=size(config.DB_PATH) + size(wal), database_schema=version,
        events=events, people=people, gallery_bytes=size(config.GALLERY_PATH),
        enrolled=len(enrollment.load_gallery().counts()), backups=backups(),
        cameras_running=sum(w.state != "stopped" for w in workers),
        device=DeviceInfo(active=dev.name, label=dev.label, requested=dev.requested, providers=dev.providers,
                          warning=dev.warning),
        camera_devices=[CameraDevice(camera=w.camera, label=w.device, warning=w.device_warning)
                        for w in workers if w.device],
        maintenance=Maintenance(
            last_run=datetime.fromisoformat(last["time"]) if last.get("time") else None, last_ok=last.get("ok"),
            last_error=last.get("error"), backup_hour=config.BACKUP_HOUR,
            backup_keep=config.BACKUP_KEEP,
            backup_copy_dir=str(config.BACKUP_COPY_DIR) if config.BACKUP_COPY_DIR else None,
            snapshot_retention_days=config.SNAPSHOT_RETENTION_DAYS, snapshots_bytes=maintenance.snapshot_bytes()))


class BackupIn(BaseModel):
    reason: str = "backup from the dashboard"


@router.post("/backup", response_model=Backup, status_code=201)
async def backup_now(body: BackupIn, request: Request, admin: CurrentUser = Depends(manage)) -> Backup:
    """Make a backup set in data/backups/: the database (consistent while cameras keep writing: SQLite's online
    backup, then an integrity check), the face gallery, the enrollment photos, cameras.json and settings.json; and
    copy it to the second backup folder if one is set. Audited. Manual sets are never deleted automatically
    (only the newest BACKUP_KEEP nightly sets are kept)."""

    def run() -> Backup:
        made = maintenance.make_backup(auth.db_of(request), maintenance.MANUAL)
        return Backup(file=made.database.name, size_bytes=made.database.stat().st_size, created=datetime.now(),
                      files=sorted(p.name for p in made.files.values()),
                      copied_to=str(made.copied_to) if made.copied_to else None, copy_error=made.copy_error)

    result = await run_in_threadpool(run)
    await run_in_threadpool(auth.db_of(request).log_change, admin.username, "backup", "database",
                            {"file": result.file, "bytes": result.size_bytes, "files": result.files,
                             "copied_to": result.copied_to, "copy_error": result.copy_error}, body.reason)
    return result
