"""Backups and snapshot retention: "Back up now" (Settings > System) and the nightly job.

While the app runs, a lifespan task checks every MAINTENANCE_CHECK_SECONDS whether the nightly job is due (once a
day, at or after BACKUP_HOUR). The job:

- backs up the database (SQLite's online backup: consistent while the cameras keep writing) and the face gallery
  to <data>/backups/ as "<name>-nightly-<time>";
- deletes nightly backups older than BACKUP_RETENTION_DAYS (manual "Back up now" copies and the copies made before
  a database upgrade are never deleted automatically);
- deletes event snapshots (data/snapshots/<day>/) older than SNAPSHOT_RETENTION_DAYS and clears those events'
  snapshot_path, so the dashboard shows "no snapshot" instead of a broken picture. The events themselves stay.

Each run is audited (user "system"); the last run is kept in data/run/_maintenance.json. A day the app was off is
caught up at the first check after the next start (only once, not once per missed day).
"""

import asyncio
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from api.db import Database
from app import config
from app.fileutil import atomic_write_bytes

NIGHTLY = "nightly"
MANUAL = "manual"


def backup_dir() -> Path:
    return config.DB_PATH.parent / "backups"


def state_path() -> Path:
    return config.RUN_DIR / "_maintenance.json"


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def backup_database(db: Database, kind: str, now: datetime | None = None) -> Path:
    """Copy the database and the face gallery to backups/ as <name>-<kind>-<time>; returns the database copy."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{config.DB_PATH.stem}-{kind}-{stamp}.db"
    with closing(db.connect()) as src, closing(sqlite3.connect(dest)) as out:
        src.backup(out)
    if config.GALLERY_PATH.is_file():
        shutil.copy2(config.GALLERY_PATH, folder / f"{config.GALLERY_PATH.stem}-{kind}-{stamp}.npz")
    return dest


def prune_backups(now: datetime, days: int | None = None) -> list[str]:
    """Delete nightly backups (database and gallery) older than `days`; returns the deleted file names."""
    days = config.BACKUP_RETENTION_DAYS if days is None else days
    folder = backup_dir()
    if not folder.is_dir():
        return []
    cutoff = now - timedelta(days=days)
    gone = []
    for path in sorted(folder.glob(f"*-{NIGHTLY}-*")):
        stamp = path.stem.rsplit(f"-{NIGHTLY}-", 1)[-1]
        try:
            made = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            continue  # not one of ours
        if made < cutoff:
            path.unlink(missing_ok=True)
            gone.append(path.name)
    return gone


def clean_snapshots(db: Database, today: date, days: int | None = None) -> dict:
    """Delete snapshot folders of days older than `days` and clear their events' snapshot_path."""
    days = config.SNAPSHOT_RETENTION_DAYS if days is None else days
    cutoff = today - timedelta(days=days)
    removed, files = [], 0
    if config.SNAPSHOT_DIR.is_dir():
        for folder in sorted(p for p in config.SNAPSHOT_DIR.iterdir() if p.is_dir()):
            try:
                day = date.fromisoformat(folder.name)
            except ValueError:
                continue
            if day < cutoff:
                files += sum(1 for f in folder.rglob("*") if f.is_file())
                shutil.rmtree(folder, ignore_errors=True)
                removed.append(folder.name)
    cleared = 0
    if removed:
        prefix = config.SNAPSHOT_DIR.relative_to(config.DATA_DIR).as_posix()
        with db.transaction() as conn:
            for day in removed:
                cleared += conn.execute("UPDATE attendance_events SET snapshot_path = NULL WHERE snapshot_path LIKE ?",
                                        (f"{prefix}/{day}/%",)).rowcount
    return {"days": removed, "files": files, "events": cleared, "retention_days": days}


def run_nightly(db: Database, now: datetime | None = None) -> dict:
    """Backup, prune, snapshot cleanup; audited; the result is also saved as the last run."""
    now = now or datetime.now()
    result: dict = {"time": now.isoformat(timespec="seconds")}
    try:
        result["backup"] = backup_database(db, NIGHTLY, now).name
        result["pruned"] = prune_backups(now)
        result["snapshots"] = clean_snapshots(db, now.date())
        db.audit("system", "backup", "database", {"file": result["backup"], "pruned": result["pruned"]},
                 "nightly backup")
        if result["snapshots"]["days"]:
            db.audit("system", "snapshot_cleanup", "snapshots", result["snapshots"],
                     f"older than {result['snapshots']['retention_days']} days")
        result["ok"] = True
    except Exception as e:  # noqa: BLE001  reported in the state file and the System page
        result.update(ok=False, error=f"{type(e).__name__}: {e}")
    state = {"last_date": now.date().isoformat(), "last": result}
    atomic_write_bytes(state_path(), json.dumps(state, indent=2).encode("utf-8"))
    return result


def due(now: datetime, state: dict) -> bool:
    """Once a day, at or after BACKUP_HOUR; a missed day is caught up once."""
    last = state.get("last_date")
    if last == now.date().isoformat():
        return False
    if last and date.fromisoformat(last) < now.date() - timedelta(days=1):
        return True  # more than a day behind (the app was off): catch up now
    return now.hour >= config.BACKUP_HOUR


async def run_loop(db: Database) -> None:
    """The lifespan task. The first check waits one interval, so a catch-up never competes with the start."""
    while True:
        await asyncio.sleep(config.MAINTENANCE_CHECK_SECONDS)
        try:
            if due(datetime.now(), read_state()):
                result = await run_in_threadpool(run_nightly, db)
                print(f"Nightly maintenance: {result}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  never end the app over this
            print(f"Nightly maintenance failed: {type(e).__name__}: {e}", flush=True)


def snapshot_bytes() -> int:
    if not config.SNAPSHOT_DIR.is_dir():
        return 0
    return sum(f.stat().st_size for f in config.SNAPSHOT_DIR.rglob("*") if f.is_file())

