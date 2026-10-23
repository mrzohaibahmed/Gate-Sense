"""Backup sets (database, gallery, known/ photos, cameras.json, settings.json), their checks, the second backup
folder, keeping the newest N, and restoring a set (api/maintenance.py, scripts/restore.py)."""

import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from api import maintenance
from api.db import Database
from app import camera_settings, config, db
from app.gallery import Gallery
from tests.test_pipeline import unit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import restore as restore_script  # noqa: E402
import stop as stop_script  # noqa: E402

GATE = {"name": "gate1", "direction": "IN", "type": "hikvision", "ip": "192.168.1.101", "port": 554,
        "user": "gatesense", "channel": 1, "stream": "main", "enabled": True}


@pytest.fixture
def site():
    """A GateSense data folder with something in every part of a backup set."""
    database = Database()
    conn = database.connect()
    db.sync_persons(conn, ["1001", "1002"])
    camera = db.upsert_camera(conn, "gate1", "rtsp://x", "IN")
    db.add_event(conn, db.active_person_id(conn, "1001"), camera, datetime(2026, 10, 22, 9), 0.8, None)
    conn.close()
    Gallery.from_people({"1001": [unit(0)], "1002": [unit(1)]}).save()
    for code in ("1001", "1002"):
        folder = config.KNOWN_DIR / code
        folder.mkdir(parents=True)
        (folder / "front.jpg").write_bytes(b"jpeg " + code.encode())
        (folder / "photos.json").write_text("{}")
    camera_settings.save([camera_settings.build(GATE, [])])
    config.SETTINGS_FILE.write_text(json.dumps({"settings": {"MATCH_THRESHOLD": 0.5}}))
    return database


def event_count() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT count(*) FROM attendance_events").fetchone()[0]
    finally:
        conn.close()


# --- Making a set --------------------------------------------------------------------------------------------


def test_a_backup_set_has_every_part_each_checked(site):
    made = maintenance.make_backup(site, maintenance.MANUAL, datetime(2026, 10, 23, 12, 0, 0))
    assert set(made.files) == set(maintenance.PARTS)
    assert sorted(p.name for p in made.files.values()) == [
        "attendance-manual-20261023-120000.db", "cameras-manual-20261023-120000.json",
        "gallery-manual-20261023-120000.npz", "known-manual-20261023-120000.zip",
        "settings-manual-20261023-120000.json"]
    assert maintenance.integrity(made.database) == "ok"
    with zipfile.ZipFile(made.files["known"]) as z:
        assert sorted(z.namelist()) == ["1001/front.jpg", "1001/photos.json", "1002/front.jpg", "1002/photos.json"]
    assert not list(maintenance.backup_dir().glob("*.partial"))
    assert not list(maintenance.backup_dir().glob("*.env*"))  # never the passwords


def test_a_database_copy_that_fails_its_check_is_not_kept(site, monkeypatch):
    monkeypatch.setattr(maintenance, "integrity", lambda path: "*** in database main *** Page 3 is never used")
    with pytest.raises(maintenance.BackupError, match="integrity check"):
        maintenance.make_backup(site, maintenance.NIGHTLY)
    assert list(maintenance.backup_dir().iterdir()) == []  # no half set that would look usable
    result = maintenance.run_nightly(site, datetime(2026, 10, 23, 2, 5))
    assert result["ok"] is False and "integrity check" in result["error"]


def test_an_unreadable_cameras_file_is_left_out_not_fatal(site):
    config.CAMERAS_FILE.write_text("{half written")
    result = maintenance.run_nightly(site, datetime(2026, 10, 23, 2, 5))
    assert result["ok"] is False and "cameras not backed up" in result["error"]
    assert result["backup"] == "attendance-nightly-20261023-020500.db" and len(result["files"]) == 4


def test_the_set_is_copied_to_the_second_folder_and_pruned_there(site, tmp_path, monkeypatch):
    usb = tmp_path / "usb" / "GateSense"
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", usb)
    monkeypatch.setattr(config, "BACKUP_KEEP", 2)
    for day in (20, 21, 22):
        result = maintenance.run_nightly(site, datetime(2026, 10, day, 2, 5))
        assert result["ok"] and result["copied_to"] == str(usb)
    assert [s.stamp for s in maintenance.backup_sets(usb)] == ["20261022-020500", "20261021-020500"]
    assert [s.stamp for s in maintenance.backup_sets()] == ["20261022-020500", "20261021-020500"]
    assert maintenance.integrity(maintenance.backup_sets(usb)[0].database) == "ok"
    row = next(e for e in site.audit_entries() if e["action"] == "backup")
    assert json.loads(row["details"])["copied_to"] == str(usb)


def test_a_failed_copy_is_reported_and_the_local_set_stays(site, tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("a file where the folder should be (a USB drive letter now used by something else)")
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", blocked)
    result = maintenance.run_nightly(site, datetime(2026, 10, 23, 2, 5))
    assert result["ok"] is False and result["error"].startswith(f"copy to {blocked} failed")
    assert maintenance.backup_sets()[0].stamp == "20261023-020500"  # the set in backups/ is fine
    assert maintenance.read_state()["last"]["ok"] is False  # shown on Settings > System


# --- Restoring ----------------------------------------------------------------------------------------------


def damage_everything():
    """What a bad day leaves: more events, a lost photo, changed cameras and settings, and a stale -wal file."""
    conn = db.connect()
    db.add_event(conn, db.active_person_id(conn, "1002"), 1, datetime(2026, 10, 23, 9), 0.8, None)
    conn.close()
    (config.KNOWN_DIR / "1001" / "front.jpg").unlink()
    camera_settings.save([camera_settings.build({**GATE, "enabled": False}, [])])
    config.SETTINGS_FILE.write_text(json.dumps({"settings": {}}))
    Gallery.from_people({"1001": [unit(0)]}).save()


def test_restore_puts_every_part_back_and_keeps_the_old_files(site):
    made = maintenance.make_backup(site, maintenance.MANUAL, datetime(2026, 10, 23, 12))
    damage_everything()
    assert event_count() == 2
    wal = config.DB_PATH.with_name(config.DB_PATH.name + "-wal")
    report = maintenance.restore_set(made, now=datetime(2026, 10, 23, 13), log=lambda m: None, user="tester",
                                     reason="test restore")
    assert report["ok"] and report["problems"] == []
    assert report["restored"] == ["database", "gallery", "known", "cameras", "settings"]
    assert event_count() == 1
    assert (config.KNOWN_DIR / "1001" / "front.jpg").read_bytes() == b"jpeg 1001"
    assert config.parse_cameras(config.CAMERAS_FILE)[0]["enabled"] is True
    assert json.loads(config.SETTINGS_FILE.read_text())["settings"] == {"MATCH_THRESHOLD": 0.5}
    assert Gallery.load().names == ["1001", "1002"]
    aside = Path(report["moved_aside_to"])
    assert aside.name == "before-restore-20261023-130000"
    assert {"attendance.db", "gallery.npz", "known", "cameras.json", "settings.json"} <= {p.name for p in aside.iterdir()}
    assert not (aside / "known" / "1001" / "front.jpg").exists()  # the damaged state, kept as it was
    assert report["checks"]["database"] == "ok" and report["checks"]["people"] == 2
    conn = db.connect()
    row = dict(db.audit_entries(conn)[0])
    conn.close()
    assert (row["user"], row["action"], row["reason"]) == ("tester", "restore_backup", "test restore")
    assert not wal.exists() or wal.stat().st_size < 1_000_000  # a fresh one from the checks, not the old one


def test_restore_moves_a_stale_wal_aside(site):
    made = maintenance.make_backup(site, maintenance.MANUAL, datetime(2026, 10, 23, 12))
    stale = config.DB_PATH.with_name(config.DB_PATH.name + "-wal")
    stale.write_bytes(b"left by the database before the restore")
    report = maintenance.restore_set(made, ("database",), log=lambda m: None)
    assert report["ok"]
    assert (Path(report["moved_aside_to"]) / "attendance.db-wal").read_bytes().startswith(b"left by")


def test_restore_refuses_a_damaged_backup_and_changes_nothing(site):
    made = maintenance.make_backup(site, maintenance.MANUAL)
    data = bytearray(made.database.read_bytes())
    data[:16] = b"this is not sql!"
    made.database.write_bytes(bytes(data))
    with pytest.raises(maintenance.BackupError, match="integrity check"):
        maintenance.restore_set(made, log=lambda m: None)
    assert event_count() == 1 and not list(maintenance.backup_dir().glob("before-restore-*"))


def test_restore_refuses_a_photos_zip_with_unsafe_paths(site):
    made = maintenance.make_backup(site, maintenance.MANUAL)
    with zipfile.ZipFile(made.files["known"], "a") as z:
        z.writestr("../../escaped.txt", "x")
    with pytest.raises(maintenance.BackupError, match="unsafe paths"):
        maintenance.restore_set(made, ("known",), log=lambda m: None)
    assert (config.KNOWN_DIR / "1001" / "front.jpg").exists()


# --- scripts/restore.py --------------------------------------------------------------------------------------


def test_restore_script_lists_and_restores_the_latest(site, capsys):
    maintenance.make_backup(site, maintenance.NIGHTLY, datetime(2026, 10, 22, 2))
    maintenance.make_backup(site, maintenance.MANUAL, datetime(2026, 10, 23, 12))
    assert restore_script.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert out.index("20261023-120000") < out.index("20261022-020000") and "database, gallery, known" in out
    damage_everything()
    assert restore_script.main(["latest", "--yes", "--only", "database,known"]) == 0
    out = capsys.readouterr().out
    assert "Restored: database, known." in out and "start.bat" in out
    assert event_count() == 1 and (config.KNOWN_DIR / "1001" / "front.jpg").exists()
    assert config.parse_cameras(config.CAMERAS_FILE)[0]["enabled"] is False  # not asked for: left alone


def test_restore_script_asks_first(site, monkeypatch, capsys):
    maintenance.make_backup(site, maintenance.MANUAL, datetime(2026, 10, 23, 12))
    damage_everything()
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert restore_script.main(["20261023-120000"]) == 1
    assert "Nothing changed." in capsys.readouterr().out and event_count() == 2


def test_restore_script_wont_restore_while_gatesense_runs(site, monkeypatch, capsys):
    maintenance.make_backup(site, maintenance.MANUAL, datetime(2026, 10, 23, 12))
    monkeypatch.setattr(restore_script, "api_pid", lambda: os.getpid())  # "running" and won't stop
    monkeypatch.setattr(stop_script, "api_pid", lambda: os.getpid())
    monkeypatch.setattr(stop_script, "TIMEOUT", 0.3)
    assert restore_script.main(["latest", "--yes"]) == 1
    assert "still running" in capsys.readouterr().out
    assert (config.RUN_DIR / "_api.stop").exists()  # it asked GateSense to stop, like stop.bat
    assert not list(maintenance.backup_dir().glob("before-restore-*"))  # and changed nothing


def test_restore_script_from_the_second_folder(site, tmp_path, monkeypatch, capsys):
    usb = tmp_path / "usb"
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", usb)
    maintenance.make_backup(site, maintenance.NIGHTLY, datetime(2026, 10, 22, 2))
    for path in maintenance.backup_dir().glob("*-nightly-*"):
        path.unlink()  # the local disk lost them
    damage_everything()
    assert restore_script.main(["latest", "--from", str(usb), "--yes"]) == 0
    assert event_count() == 1


def test_older_backup_is_upgraded_on_restore(site):
    """A set from before a schema step: restored, then upgraded like any older database (with its own copy)."""
    made = maintenance.make_backup(site, maintenance.MANUAL, datetime(2026, 10, 23, 12))
    with sqlite3.connect(made.database) as conn:
        conn.execute("DROP TABLE user_preferences")
        conn.execute("PRAGMA user_version = 2")
    report = maintenance.restore_set(made, ("database",), log=lambda m: None)
    assert report["ok"]
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.VERSIONS)
    conn.close()
