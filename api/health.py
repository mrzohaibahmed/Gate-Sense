"""What GET /api/health reports besides the database: each camera, free disk space and the last nightly backup, with
the problems among them in plain words (the Overview shows them in a warning banner; monitoring can read them).

A problem is something someone should look at: a camera with Auto on that isn't running (or isn't sending
pictures), less than HEALTH_MIN_FREE_BYTES free on the data disk, a nightly backup that failed or is overdue.
A camera that is off on purpose (Auto off, or stopped by hand) is not a problem.
"""

import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from api import maintenance
from app import config


class CameraHealth(BaseModel):
    name: str
    state: str = Field(description="running, starting, not responding or stopped")
    enabled: bool = Field(description="Auto: started with GateSense and restarted when it stops")
    auto_paused: bool = Field(description="Auto is on but it was stopped by hand")
    last_frame_age_seconds: float | None = Field(description="seconds since the camera's newest frame; None = none")
    problem: str | None = None


class DiskHealth(BaseModel):
    path: str
    free_bytes: int
    total_bytes: int
    problem: str | None = None


class BackupHealth(BaseModel):
    last_run: datetime | None
    ok: bool | None
    error: str | None
    problem: str | None = None


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def camera_health(workers, supervisor, now: datetime) -> list[CameraHealth]:
    enabled = {c["name"]: bool(c.get("enabled")) for c in workers.cameras}
    out = []
    for info in workers.statuses():
        age = None if info.last_frame_at is None else round((now - info.last_frame_at).total_seconds(), 1)
        auto = enabled.get(info.camera, False)
        paused = auto and supervisor.paused(info.camera)
        problem = None
        if info.state == "not responding":
            problem = f"{info.camera} is not responding"
        elif info.state == "stopped" and auto and not paused:
            problem = f"{info.camera} is stopped (Auto is on: it is being restarted)" + (
                f"; last error: {info.last_error}" if info.last_error else "")
        elif info.state == "running":
            since = (now - info.started).total_seconds() if info.started else 0
            if age is not None and age > config.HEALTH_FRAME_STALE_SECONDS:
                problem = f"{info.camera}: no picture for {age:.0f} s" + (
                    f" ({info.warning})" if info.warning else "")
            elif age is None and since > config.HEALTH_FRAME_STALE_SECONDS:
                problem = f"{info.camera}: no picture since it started {since:.0f} s ago"
        out.append(CameraHealth(name=info.camera, state=info.state, enabled=auto, auto_paused=paused,
                                last_frame_age_seconds=age, problem=problem))
    return out


def disk_health(path: Path | None = None) -> DiskHealth:
    path = Path(path or config.DATA_DIR)
    probe = path if path.exists() else config.ROOT_DIR
    usage = shutil.disk_usage(probe)
    problem = None
    if usage.free < config.HEALTH_MIN_FREE_BYTES:
        problem = (f"only {gb(usage.free)} free on the data disk ({probe}); snapshots, logs and backups need room "
                   f"(warning below {gb(config.HEALTH_MIN_FREE_BYTES)})")
    return DiskHealth(path=str(probe), free_bytes=usage.free, total_bytes=usage.total, problem=problem)


def backup_health(uptime_seconds: float, now: datetime, state: dict | None = None) -> BackupHealth:
    """The last nightly run, and whether it failed or is overdue. Overdue = due (maintenance.due) for longer than two
    maintenance checks while the app has been running that long (a PC that was off catches up after its start)."""
    state = maintenance.read_state() if state is None else state
    last = state.get("last") or {}
    last_run = datetime.fromisoformat(last["time"]) if last.get("time") else None
    problem = None
    if last and not last.get("ok"):
        problem = f"the last nightly backup ({last_run:%Y-%m-%d %H:%M}) failed: {last.get('error') or 'unknown error'}"
    elif maintenance.due(now, state):
        grace = 2 * config.MAINTENANCE_CHECK_SECONDS
        due_today = datetime.combine(now.date(), datetime.min.time()) + timedelta(hours=config.BACKUP_HOUR)
        behind = state.get("last_date") and date.fromisoformat(state["last_date"]) < now.date() - timedelta(days=1)
        if uptime_seconds > grace and (behind or (now - due_today).total_seconds() > grace):
            problem = ("the nightly backup is overdue (last one: "
                       + (f"{last_run:%Y-%m-%d %H:%M})" if last_run else "never)"))
    return BackupHealth(last_run=last_run, ok=last.get("ok"), error=last.get("error"), problem=problem)
