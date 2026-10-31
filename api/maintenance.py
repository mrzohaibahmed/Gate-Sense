"""Backups, restore and snapshot retention: "Back up now" (Settings > System), the nightly job and scripts/restore.py.

While the app runs, a lifespan task checks every MAINTENANCE_CHECK_SECONDS whether the nightly job is due (once a
day, at or after BACKUP_HOUR). The job:

- makes a backup set in <data>/backups/, every file named "<part>-nightly-<time>": the database (SQLite's online
  backup: consistent while the cameras keep writing, then checked with PRAGMA integrity_check; on SQL Server
  <database>-nightly-<time>.bak, a COPY_ONLY backup with checksums written by the SQL Server service, then checked
  with RESTORE VERIFYONLY), the face gallery,
  the enrollment photos (known/ as a zip), cameras.json and settings.json. Never .env: it holds passwords. Each
  file is written under a .partial name, checked and flushed to disk before it gets its real name, so a set that
  looks complete is complete;
- copies the set to BACKUP_COPY_DIR (GATESENSE_BACKUP_COPY_DIR: another disk or a USB drive), if set, and checks
  the database copy there too. A copy that fails (drive unplugged) makes the run "not ok" on the System page, but
  the set in backups/ stays;
- keeps the newest BACKUP_KEEP nightly sets in each folder (manual "Back up now" sets and the copies made before
  a database upgrade are never deleted automatically);
- deletes event snapshots (data/snapshots/<day>/) older than SNAPSHOT_RETENTION_DAYS and clears those events'
  snapshot_path, so the dashboard shows "no snapshot" instead of a broken picture. The events themselves stay;
- deletes unknown visitors (app/visitors.py) first seen more than UNKNOWN_RETENTION_DAYS ago, rows and snapshots
  (data/snapshots/unknown/<day>/), whatever their review status: they are not attendance and aren't kept longer.

Each run is audited (user "system"); the last run is kept in data/run/_maintenance.json. A day the app was off is
caught up at the first check after the next start (only once, not once per missed day).

restore_set() puts a set back (scripts/restore.py stops GateSense first): the current files are moved aside to
backups/before-restore-<time>/, the database's -wal and -shm files go with them, and the restored files are
checked afterwards. A SQL Server backup (.bak) is put back by an administrator (restore_commands: the app's login
can't replace its own database); restore_set restores the other parts and refuses that one.
"""

import asyncio
import json
import os
import re
import hashlib
import shutil
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath

from starlette.concurrency import run_in_threadpool

from api.db import Database
from app import config, dbconn
from app import db as app_db
from app.fileutil import atomic_write_bytes, replace_retry, sync_dir

NIGHTLY = "nightly"
MANUAL = "manual"
KINDS = (NIGHTLY, MANUAL)
PARTS = ("database", "gallery", "known", "cameras", "settings")  # what a backup set can hold
SET_RE = re.compile(r"^(?P<stem>.+)-(?P<kind>nightly|manual)-(?P<stamp>\d{8}-\d{6})\.(?P<ext>[A-Za-z0-9]+)$")


class BackupError(RuntimeError):
    pass


def backup_dir() -> Path:
    return config.DB_PATH.parent / "backups"


def state_path() -> Path:
    return config.RUN_DIR / "_maintenance.json"


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def part_names() -> dict[str, tuple[str, str]]:
    """part -> (file stem, extension) of its file in a set, e.g. database -> ("attendance", "db"), or on SQL Server
    ("GateSense", "bak")."""
    if url := dbconn.server_url():
        from app import mssql

        database = (mssql.database_name(url), "bak")
    else:
        database = (config.DB_PATH.stem, "db")
    return {"database": database, "gallery": (config.GALLERY_PATH.stem, "npz"),
            "known": ("known", "zip"), "cameras": (config.CAMERAS_FILE.stem, "json"),
            "settings": (config.SETTINGS_FILE.stem, "json")}


def is_server_backup(path: Path) -> bool:
    """A SQL Server backup file (.bak, also while it is a .partial), not a SQLite database."""
    return ".bak" in path.suffixes


def integrity(path: Path) -> str:
    """PRAGMA integrity_check of a database file, read-only: "ok" or SQLite's description of the damage. A SQL
    Server backup is checked by the server (RESTORE VERIFYONLY)."""
    if is_server_backup(path):
        if not (url := dbconn.server_url()):
            return f"{path.name} is a SQL Server backup: set {config.DATABASE_URL_ENV} to check or restore it"
        from app import mssql

        return mssql.verify_backup(url, path)
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"
    return "; ".join(str(r[0]) for r in rows[:5])


def _fsync(path: Path) -> None:
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def _finish(partial: Path, final: Path) -> None:
    """Give a checked file its real name, on disk first."""
    _fsync(partial)
    replace_retry(partial, final)
    sync_dir(final.parent)


def zip_folder(folder: Path, dest: Path) -> int:
    """Every file under `folder` into the zip `dest` (stored: photos don't compress); returns how many."""
    count = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder).as_posix())
                count += 1
    return count


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _check_file(part: str, path: Path, source: Path | None = None) -> None:
    """Raise BackupError if a written backup file can't be read back. A copy of a SQL Server backup (`source` =
    the checked original) is compared byte for byte: the SQL Server service may not be allowed to read the copy
    folder (a USB drive)."""
    if part == "database" and source is not None and is_server_backup(path):
        if sha256(path) != sha256(source):
            raise BackupError(f"the copy {path.name} differs from {source}")
    elif part == "database":
        if (result := integrity(path)) != "ok":
            raise BackupError(f"the database copy {path.name} failed the integrity check: {result}")
    elif part == "gallery":
        from app.gallery import LOAD_ERRORS, Gallery

        try:
            Gallery.load(path)
        except LOAD_ERRORS as e:
            raise BackupError(f"the gallery copy {path.name} can't be read: {e}") from None
    elif part == "known":
        try:
            with zipfile.ZipFile(path) as z:
                if bad := z.testzip():
                    raise BackupError(f"{path.name}: {bad} is damaged")
        except zipfile.BadZipFile as e:
            raise BackupError(f"{path.name} is not a readable zip: {e}") from None
    elif part == "cameras":
        try:
            config.parse_cameras(path)
        except config.CamerasFileError as e:
            raise BackupError(f"the copy {path.name} can't be read: {e}") from None
    else:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise BackupError(f"the copy {path.name} can't be read: {e}") from None


@dataclass
class BackupSet:
    kind: str
    stamp: str
    folder: Path
    files: dict[str, Path] = field(default_factory=dict)  # part -> file
    copied_to: Path | None = None
    copy_error: str | None = None
    skipped: dict[str, str] = field(default_factory=dict)  # part -> why it isn't in the set (unreadable source)

    @property
    def database(self) -> Path:
        return self.files["database"]

    @property
    def created(self) -> datetime:
        return datetime.strptime(self.stamp, "%Y%m%d-%H%M%S")


def make_backup(db: Database, kind: str, now: datetime | None = None, folder: Path | None = None,
                copy_dir: Path | None | bool = True) -> BackupSet:
    """Write a checked backup set to `folder` (default backups/) and copy it to `copy_dir` (default
    BACKUP_COPY_DIR; False = none). A failed copy is reported in copy_error, never raised."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = folder or backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    names = part_names()
    out = BackupSet(kind, stamp, folder)
    made: list[Path] = []
    try:
        for part in PARTS:
            stem, ext = names[part]
            final = folder / f"{stem}-{kind}-{stamp}.{ext}"
            partial = final.with_name(final.name + ".partial")
            source = {"gallery": config.GALLERY_PATH, "known": config.KNOWN_DIR, "cameras": config.CAMERAS_FILE,
                      "settings": config.SETTINGS_FILE}.get(part)
            if source is not None and not source.exists():
                continue  # nothing of this part yet (no one enrolled, no settings changed)
            made.append(partial)  # first, so a failure below removes it too
            if part == "database" and db.backend == dbconn.MSSQL:
                from app import mssql

                mssql.backup_database(db.url, partial)
            elif part == "database":
                with closing(db.connect()) as src, closing(sqlite3.connect(partial)) as dst:
                    src.backup(dst)
            elif part == "known":
                zip_folder(config.KNOWN_DIR, partial)
            else:
                shutil.copy2(source, partial)
            try:
                _check_file(part, partial)
            except BackupError as e:
                if part in ("database", "gallery", "known"):
                    raise
                # A settings file that can't be read isn't worth keeping, and mustn't cost the rest of the set.
                partial.unlink(missing_ok=True)
                made.pop()
                out.skipped[part] = str(e)
                continue
            _finish(partial, final)
            made[-1] = final
            out.files[part] = final
    except BaseException:
        for path in made:  # never leave half a set behind: it would look like a usable backup
            path.unlink(missing_ok=True)
        raise
    target = config.BACKUP_COPY_DIR if copy_dir is True else (copy_dir or None)
    if target:
        try:
            copy_set(out, Path(target))
            out.copied_to = Path(target)
        except (OSError, BackupError) as e:
            out.copy_error = f"copy to {target} failed: {type(e).__name__}: {e}"
    return out


def copy_set(backup: BackupSet, target: Path) -> None:
    """Copy a set's files to another folder; each checked there before it gets its real name."""
    target.mkdir(parents=True, exist_ok=True)
    for part, path in backup.files.items():
        final = target / path.name
        partial = final.with_name(final.name + ".partial")
        try:
            shutil.copy2(path, partial)
            _check_file(part, partial, source=path)
            _finish(partial, final)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise


def backup_database(db: Database, kind: str, now: datetime | None = None) -> Path:
    """Make a backup set (see make_backup); returns the database copy."""
    return make_backup(db, kind, now).database


def backup_sets(folder: Path | None = None) -> list[BackupSet]:
    """The nightly and manual sets in a folder, newest first (.partial files are not part of any set)."""
    folder = folder or backup_dir()
    if not folder.is_dir():
        return []
    parts = {f"{stem}.{ext}": part for part, (stem, ext) in part_names().items()}
    sets: dict[tuple[str, str], BackupSet] = {}
    for path in folder.iterdir():
        m = SET_RE.match(path.name)
        if not m or not path.is_file():
            continue
        part = parts.get(f"{m['stem']}.{m['ext']}")
        if part is None:
            continue
        s = sets.setdefault((m["kind"], m["stamp"]), BackupSet(m["kind"], m["stamp"], folder))
        s.files[part] = path
    return sorted((s for s in sets.values() if "database" in s.files), key=lambda s: s.stamp, reverse=True)


def prune_backups(folder: Path | None = None, keep: int | None = None) -> list[str]:
    """Keep the newest `keep` (BACKUP_KEEP) nightly sets in a folder; returns the deleted file names. Also removes
    files left half-written by an interrupted backup."""
    keep = config.BACKUP_KEEP if keep is None else keep
    folder = folder or backup_dir()
    if not folder.is_dir():
        return []
    gone = []
    for path in folder.glob("*.partial"):
        path.unlink(missing_ok=True)
        gone.append(path.name)
    stamps = sorted({m["stamp"] for p in folder.iterdir() if (m := SET_RE.match(p.name)) and m["kind"] == NIGHTLY},
                    reverse=True)
    old = set(stamps[keep:])
    for path in sorted(folder.iterdir()):
        m = SET_RE.match(path.name)
        if m and m["kind"] == NIGHTLY and m["stamp"] in old:
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
            column = dbconn.like(dbconn.dialect(conn), "snapshot_path")
            for day in removed:
                cleared += conn.execute(f"UPDATE attendance_events SET snapshot_path = NULL WHERE {column} LIKE ?",
                                        (f"{prefix}/{day}/%",)).rowcount
    return {"days": removed, "files": files, "events": cleared, "retention_days": days}


def clean_unknown_visits(db: Database, today: date, days: int | None = None) -> dict:
    """Delete the unknown visits first seen before the day `days` ago, with their snapshots, and any leftover
    snapshot folder of those days (data/snapshots/unknown/<day>/: e.g. a visit the camera never got to write)."""
    days = config.UNKNOWN_RETENTION_DAYS if days is None else days
    cutoff = today - timedelta(days=days)
    with db.conn() as conn:
        paths = app_db.delete_unknown_visits_before(conn, datetime.combine(cutoff, time()))
    folder = config.SNAPSHOT_DIR / config.UNKNOWN_SNAPSHOT_FOLDER
    base, files = folder.resolve(), 0
    for rel in filter(None, paths):
        path = (config.DATA_DIR / rel).resolve()
        if base in path.parents and path.is_file():  # only ever a visitor snapshot
            path.unlink(missing_ok=True)
            files += 1
    removed = []
    if folder.is_dir():
        for day_folder in sorted(p for p in folder.iterdir() if p.is_dir()):
            try:
                day = date.fromisoformat(day_folder.name)
            except ValueError:
                continue
            if day < cutoff:
                files += sum(1 for f in day_folder.rglob("*") if f.is_file())
                shutil.rmtree(day_folder, ignore_errors=True)
                removed.append(day_folder.name)
    return {"visits": len(paths), "files": files, "days": removed, "retention_days": days}


def run_nightly(db: Database, now: datetime | None = None) -> dict:
    """Backup set (and its copy), pruning, snapshot and unknown visitor cleanup; audited; the result is also saved
    as the last run."""
    now = now or datetime.now()
    result: dict = {"time": now.isoformat(timespec="seconds")}
    try:
        backup = make_backup(db, NIGHTLY, now)
        result["backup"] = backup.database.name
        result["files"] = sorted(p.name for p in backup.files.values())
        result["copied_to"] = str(backup.copied_to) if backup.copied_to else None
        if backup.skipped:
            result["skipped"] = backup.skipped
        result["pruned"] = prune_backups()
        if backup.copied_to:
            result["pruned"] += prune_backups(backup.copied_to)
        result["snapshots"] = clean_snapshots(db, now.date())
        result["unknowns"] = clean_unknown_visits(db, now.date())
        db.audit("system", "backup", "database", {"file": result["backup"], "files": result["files"],
                                                  "copied_to": result["copied_to"], "copy_error": backup.copy_error,
                                                  "skipped": backup.skipped,
                                                  "pruned": result["pruned"]}, "nightly backup")
        if result["snapshots"]["days"]:
            db.audit("system", "snapshot_cleanup", "snapshots", result["snapshots"],
                     f"older than {result['snapshots']['retention_days']} days")
        if result["unknowns"]["visits"] or result["unknowns"]["days"]:
            db.audit("system", "unknown_cleanup", "unknown_visits", result["unknowns"],
                     f"older than {result['unknowns']['retention_days']} days")
        problems = [backup.copy_error] if backup.copy_error else []
        problems += [f"{part} not backed up: {why}" for part, why in backup.skipped.items()]
        result["ok"] = not problems
        if problems:
            result["error"] = "; ".join(problems)
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


# --- Restore (scripts/restore.py, with GateSense stopped) -----------------------------------------------------


def _safe_member(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and ":" not in name and "\\" not in name


def _copy_in(source: Path, dest: Path) -> None:
    """Copy a backup file over a live one: flushed to disk, then renamed into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".restoring")
    shutil.copy2(source, partial)
    _finish(partial, dest)


def restore_set(backup: BackupSet, parts: tuple[str, ...] = PARTS, now: datetime | None = None,
                log=print, user: str = "restore.py", reason: str = "restored from a backup") -> dict:
    """Put a backup set's files back. GateSense and every camera worker must be stopped (scripts/restore.py does
    that). The files it replaces, and the database's -wal/-shm files, are moved to backups/before-restore-<time>/
    first. Returns a report: what was restored, where the old files went, the checks afterwards, and problems
    (empty = ok). The restore is audited in the restored database."""
    from app import db as app_db
    from app.gallery import LOAD_ERRORS, Gallery

    parts = tuple(p for p in parts if p in backup.files)
    if "database" in parts and is_server_backup(backup.database):
        raise BackupError(f"{backup.database.name} is a SQL Server backup: an administrator restores it (see "
                          "scripts/restore.py); restore the other parts with --only")
    if "database" in parts and (result := integrity(backup.database)) != "ok":
        raise BackupError(f"the backup {backup.database.name} failed the integrity check ({result}); "
                          "choose another backup")
    if "known" in parts:
        with zipfile.ZipFile(backup.files["known"]) as z:
            if bad := z.testzip():
                raise BackupError(f"{backup.files['known'].name}: {bad} is damaged; choose another backup")
            if unsafe := [n for n in z.namelist() if not _safe_member(n)]:
                raise BackupError(f"{backup.files['known'].name} has unsafe paths: {unsafe[:3]}")

    aside = backup_dir() / f"before-restore-{(now or datetime.now()):%Y%m%d-%H%M%S}"
    aside.mkdir(parents=True, exist_ok=True)

    def move_aside(path: Path) -> None:
        if path.exists():
            shutil.move(str(path), str(aside / path.name))
            log(f"  moved the current {path.name} to {aside}")

    report: dict = {"backup": f"{backup.kind} {backup.stamp}", "restored": [], "moved_aside_to": str(aside)}
    if "database" in parts:
        for suffix in ("", "-wal", "-shm"):  # an old -wal next to a restored database would be replayed into it
            move_aside(config.DB_PATH.with_name(config.DB_PATH.name + suffix))
        _copy_in(backup.database, config.DB_PATH)
        report["restored"].append("database")
    if "gallery" in parts:
        move_aside(config.GALLERY_PATH)
        _copy_in(backup.files["gallery"], config.GALLERY_PATH)
        report["restored"].append("gallery")
    if "known" in parts:
        move_aside(config.KNOWN_DIR)
        config.KNOWN_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(backup.files["known"]) as z:
            z.extractall(config.KNOWN_DIR)
        report["restored"].append("known")
    for part, path in (("cameras", config.CAMERAS_FILE), ("settings", config.SETTINGS_FILE)):
        if part in parts:
            move_aside(path)
            _copy_in(backup.files[part], path)
            report["restored"].append(part)

    checks: dict = {}
    problems: list[str] = []
    server = dbconn.server_url()
    if server or config.DB_PATH.is_file():
        # SQLite: upgrades an older backup's schema (copy first). SQL Server: the database in use (not restored here).
        with closing(app_db.connect(None if server else config.DB_PATH)) as conn:
            if not server:
                checks["database"] = integrity(config.DB_PATH)
                if checks["database"] != "ok":
                    problems.append(f"database: {checks['database']}")
            checks["events"] = conn.execute("SELECT count(*) FROM attendance_events").fetchone()[0]
            checks["people"] = conn.execute("SELECT count(*) FROM persons").fetchone()[0]
            checks["users"] = conn.execute("SELECT count(*) FROM users").fetchone()[0]
            if report["restored"]:
                app_db.log_change(conn, user, "restore_backup", "database",
                                  {"backup": report["backup"], "parts": report["restored"], "moved_aside_to": str(aside)},
                                  reason)
    if config.GALLERY_PATH.is_file():
        try:
            checks["gallery"] = f"ok, {len(Gallery.load())} people"
        except LOAD_ERRORS as e:
            problems.append(f"gallery: {e}")
    if config.CAMERAS_FILE.is_file():
        try:
            checks["cameras"] = f"ok, {len(config.parse_cameras(config.CAMERAS_FILE))} cameras"
        except config.CamerasFileError as e:
            problems.append(f"cameras: {e}")
    if config.KNOWN_DIR.is_dir():
        checks["known"] = f"{sum(1 for p in config.KNOWN_DIR.iterdir() if p.is_dir())} people's photo folders"
    report["checks"], report["problems"], report["ok"] = checks, problems, not problems
    return report
