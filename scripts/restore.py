"""Restore GateSense from a backup set: the database, the face gallery, the enrollment photos (known/),
cameras.json and settings.json.

    .venv\\Scripts\\python scripts\\restore.py --list                       # the backup sets, newest first
    .venv\\Scripts\\python scripts\\restore.py latest                       # the newest set
    .venv\\Scripts\\python scripts\\restore.py 20261023-020005              # a set by its time
    .venv\\Scripts\\python scripts\\restore.py latest --from E:\\GateSense   # from the second backup folder
    .venv\\Scripts\\python scripts\\restore.py latest --only database,gallery

What it does:
1. Checks the backup (the database with PRAGMA integrity_check, the photos zip) before touching anything.
2. Stops GateSense and every camera worker cleanly (like stop.bat), and refuses if any is still running.
3. Moves the current files aside to data/backups/before-restore-<time>/ (with the database's -wal and -shm files,
   which must never be left next to a restored database), then copies the backup in.
4. Checks the result (integrity, counts, gallery, cameras) and writes the restore to the audit log.
Then start GateSense again with start.bat. .env (passwords) is never in a backup: keep your own copy of it.

On SQL Server (GATESENSE_DB_URL) the database part is a .bak file that an administrator restores (GateSense's own
login can't replace its database): this checks it (RESTORE VERIFYONLY) and prints the T-SQL to run, then the
command for the other parts (--only gallery,known,cameras,settings).
"""

import argparse
import getpass
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import maintenance  # noqa: E402
from api.workers import api_pid  # noqa: E402
from app import config, dbconn, workers  # noqa: E402

import stop  # noqa: E402  scripts/stop.py, next to this file


def running_workers() -> list[str]:
    """Cameras whose worker process is alive, found from the status files (cameras.json may be the broken part)."""
    if not config.RUN_DIR.is_dir():
        return []
    names = [p.stem for p in config.RUN_DIR.glob("*.json") if not p.stem.startswith("_")]
    return [n for n in names if workers.live_status(n)]


def stop_everything() -> bool:
    """Stop camera workers and the app (stop.bat); True if nothing is running afterwards."""
    if api_pid() or running_workers():
        print("Stopping GateSense first...")
        for name in running_workers():  # whoever started them, like stop.bat
            workers.stop_path(name).touch()
        stop.stop_process("GateSense", api_pid(), "_api")
        deadline = time.monotonic() + stop.TIMEOUT
        while running_workers() and time.monotonic() < deadline:
            time.sleep(0.2)
    if pid := api_pid():
        print(f"GateSense is still running (process {pid}); close it, then try again.")
        return False
    if left := running_workers():
        print(f"Camera workers still running: {', '.join(left)}; stop them, then try again.")
        return False
    return True


def describe(s: maintenance.BackupSet) -> str:
    parts = ", ".join(p for p in maintenance.PARTS if p in s.files)
    size = sum(p.stat().st_size for p in s.files.values()) / 1e6
    return f"{s.stamp}  {s.kind:<7}  {size:8.1f} MB  {parts}"


def choose(sets: list[maintenance.BackupSet], which: str) -> maintenance.BackupSet | None:
    if which == "latest":
        return sets[0] if sets else None
    return next((s for s in sets if s.stamp == which or which in {p.name for p in s.files.values()}), None)


def server_restore(chosen: maintenance.BackupSet, parts: tuple[str, ...]) -> int:
    """A SQL Server backup: check it and print what an administrator runs (nothing is changed here)."""
    from app import mssql

    url = dbconn.server_url()
    if not url:
        print(f"{chosen.database.name} is a SQL Server backup: set {config.DATABASE_URL_ENV} to restore it.")
        return 1
    if (result := maintenance.integrity(chosen.database)) != "ok":
        print(f"ERROR: this backup's database fails RESTORE VERIFYONLY ({result}). Choose another one.")
        return 1
    others = [p for p in parts if p != "database"]
    print(f"{chosen.database.name} is a SQL Server backup (checked: RESTORE VERIFYONLY ok). An administrator restores "
          "it; GateSense's own login can't replace its database.")
    print("1. Stop GateSense: stop.bat")
    print("2. In SQL Server Management Studio or sqlcmd, as an administrator of the SQL Server instance, run:\n")
    print(mssql.restore_commands(url, chosen.database.resolve()))
    print("\n   (The SQL Server service must be able to read that file: it wrote it, unless it was copied from the "
          "second backup folder: then copy it back to data\\backups first.)")
    if others:
        print("3. Then restore the other parts of the set:\n"
              f"   .venv\\Scripts\\python scripts\\restore.py {chosen.stamp} --only {','.join(others)}")
    print("Then start GateSense again with start.bat.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts\\restore.py", description=__doc__.splitlines()[0])
    parser.add_argument("backup", nargs="?", help='"latest", or a set\'s time (as --list shows it) or file name')
    parser.add_argument("--list", action="store_true", help="list the backup sets and exit")
    parser.add_argument("--from", dest="folder", type=Path,
                        help="the folder with the backups (default data/backups; e.g. the second backup folder)")
    parser.add_argument("--only", help=f"comma-separated parts to restore: {','.join(maintenance.PARTS)}")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    parser.add_argument("--reason", default="restored from a backup", help="for the audit log")
    args = parser.parse_args(argv)

    folder = args.folder or maintenance.backup_dir()
    sets = maintenance.backup_sets(folder)
    if args.list or not args.backup:
        print(f"Backup sets in {folder} (newest first):" if sets else f"No backup sets in {folder}.")
        for s in sets:
            print("  " + describe(s))
        if not args.list:
            print('\nRestore one with: .venv\\Scripts\\python scripts\\restore.py <time>   (or "latest")')
        return 0
    chosen = choose(sets, args.backup)
    if chosen is None:
        print(f"No backup set {args.backup!r} in {folder}. See --list.")
        return 1
    parts = tuple(maintenance.PARTS)
    if args.only:
        parts = tuple(p.strip() for p in args.only.split(","))
        if unknown := [p for p in parts if p not in maintenance.PARTS]:
            print(f"Unknown part(s) {unknown}; choose from {', '.join(maintenance.PARTS)}.")
            return 1
    parts = tuple(p for p in parts if p in chosen.files)

    print(f"Backup {describe(chosen)}")
    if "database" in parts and maintenance.is_server_backup(chosen.database):
        return server_restore(chosen, parts)
    if "database" in parts and (result := maintenance.integrity(chosen.database)) != "ok":
        print(f"ERROR: this backup's database fails the integrity check ({result}). Choose another one.")
        return 1
    print(f"This replaces: {', '.join(parts)}. The current files are moved to {maintenance.backup_dir()}"
          f"\\before-restore-<time>\\ first, so nothing is lost.")
    if not args.yes and input("Restore it now? GateSense is stopped first. [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing changed.")
        return 1
    if not stop_everything():
        return 1
    try:
        report = maintenance.restore_set(chosen, parts, user=f"restore.py ({getpass.getuser()})", reason=args.reason)
    except maintenance.BackupError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Restored: {', '.join(report['restored'])}. The files it replaced are in {report['moved_aside_to']}.")
    for key, value in report["checks"].items():
        print(f"  {key}: {value}")
    for problem in report["problems"]:
        print(f"  PROBLEM: {problem}")
    if not report["ok"]:
        print("The restored files have problems (above). Try another backup, or put the old files back from "
              f"{report['moved_aside_to']}.")
        return 1
    print("Done. Start GateSense again with start.bat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
