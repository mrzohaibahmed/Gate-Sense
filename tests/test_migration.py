"""app.db's numbered schema steps: an existing database keeps every row and gains users, sessions and sites."""

import sqlite3

from app import config, db

OLD_TABLES = ("persons", "cameras", "attendance_events", "daily_attendance", "audit_log")


def old_database(path):
    """A database as the app made it before step 1 (user_version 0), with some data."""
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO persons (employee_code, name, department) VALUES ('1234', 'Waqas', 'Stores')")
    conn.execute("INSERT INTO persons (employee_code, name, active) VALUES ('5555', 'Ali', 0)")
    conn.execute("INSERT INTO cameras (name, source, direction) VALUES ('entrance', '0', 'IN')")
    conn.execute("INSERT INTO cameras (name, source, direction) VALUES ('exit', 'rtsp://u:***@10.0.0.2/1', 'OUT')")
    conn.execute("INSERT INTO attendance_events (person_id, camera_id, timestamp, match_score, snapshot_path, voided) "
                 "VALUES (1, 1, '2026-10-20 09:01:00', 0.71, 'snapshots/a.jpg', 0)")
    conn.execute("INSERT INTO attendance_events (person_id, camera_id, timestamp, match_score, manual) "
                 "VALUES (1, 2, '2026-10-20 17:30:00', 0, 1)")
    conn.execute("INSERT INTO daily_attendance VALUES (1, '2026-10-20', '09:01:00', '17:30:00', 8.48, 'present')")
    conn.execute("INSERT INTO audit_log (timestamp, user, action, target, details, reason) "
                 "VALUES ('2026-10-20 18:00:00', 'admin', 'add_manual_event', 'event:2', '{}', 'forgot to scan')")
    conn.commit()
    conn.close()


def dump(path, tables=OLD_TABLES) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}
    finally:
        conn.close()


def test_existing_database_keeps_its_data(tmp_path, capsys):
    path = tmp_path / "attendance.db"
    old_database(path)
    before = dump(path)

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.VERSIONS) == 3
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"users", "sessions", "sites"} <= tables
    sites = [tuple(r) for r in conn.execute("SELECT id, name, timezone FROM sites")]
    assert sites == [(1, config.DEFAULT_SITE_NAME, config.DEFAULT_SITE_TIMEZONE)]
    assert [r["site_id"] for r in conn.execute("SELECT site_id FROM cameras ORDER BY id")] == [1, 1]
    assert [r["site_id"] for r in conn.execute("SELECT site_id FROM persons ORDER BY id")] == [1, 1]
    assert [r[0] for r in conn.execute("SELECT original_timestamp FROM attendance_events")] == [None, None]
    conn.close()

    after = dump(path)
    for table in OLD_TABLES:  # the same rows; cameras, persons and events have one new column at the end
        grown = table in ("cameras", "persons", "attendance_events")
        assert [row[:-1] if grown else row for row in after[table]] == before[table], table

    backups = list((tmp_path / "backups").glob("attendance-v0-*.db"))
    assert len(backups) == 1 and dump(backups[0]) == before  # the untouched copy
    assert "upgraded from version 0 to 3" in capsys.readouterr().out

    db.connect(path).close()  # already upgraded: nothing runs, no second copy
    assert len(list((tmp_path / "backups").iterdir())) == 1 and dump(path) == after


def test_new_database_gets_everything_without_a_backup(tmp_path):
    path = tmp_path / "new.db"
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    cam = db.upsert_camera(conn, "gate", 0, "IN")
    db.ensure_cameras(conn, [{"name": "gate2", "source": 1, "direction": "OUT"}])
    assert [r["site_id"] for r in conn.execute("SELECT site_id FROM cameras ORDER BY id")] == [1, 1] and cam == 1
    conn.close()
    assert not (tmp_path / "backups").exists()


def test_connect_without_migrate_skips_the_checks(tmp_path):
    path = tmp_path / "x.db"
    db.connect(path).close()
    conn = db.connect(path, migrate=False)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_a_correction_keeps_the_original_time(tmp_path):
    from datetime import datetime

    conn = db.connect(tmp_path / "c.db")
    db.sync_persons(conn, ["1234"])
    cam = db.upsert_camera(conn, "entrance", 0, "IN")
    pid = db.person_ids(conn)["1234"]
    assert conn.execute("SELECT site_id FROM persons WHERE id = ?", (pid,)).fetchone()[0] == 1
    eid = db.add_event(conn, pid, cam, datetime(2026, 10, 20, 9, 1), 0.7, None)
    db.edit_event_time(conn, eid, datetime(2026, 10, 20, 8, 55), "badge was earlier", "hr1")
    db.edit_event_time(conn, eid, datetime(2026, 10, 20, 8, 50), "checked the CCTV", "hr1")
    row = conn.execute("SELECT timestamp, original_timestamp FROM attendance_events WHERE id = ?", (eid,)).fetchone()
    assert tuple(row) == ("2026-10-20 08:50:00", "2026-10-20 09:01:00")  # the first time is kept
    conn.close()
