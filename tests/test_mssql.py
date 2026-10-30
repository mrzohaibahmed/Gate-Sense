"""SQL Server (app/mssql.py, app/dbconn.py, scripts/migrate_sqlite_to_mssql.py).

Without a server: the setting (GATESENSE_DB_URL, the old name refused, the password never shown), values read back
as SQLite returns them, and the SQL Server schema against SQLite's (same tables, columns, keys and indexes). With
GATESENSE_TEST_DB_URL (tests/conftest.py: a separate test database, emptied before each test): the same days,
events and audit details as SQLite for one scenario, one writer at a time, a lost connection replaced, an event
whose COMMIT answer was lost recorded once, backups, and the migration script.
"""

import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from api import maintenance
from api.db import Database
from api.main import startup_problems
from app import config, db, dbconn, reports
from app.gallery import Gallery, Match
from app.shifts import Shift
from app.tracker import Sample, TrackState
from tests.conftest import TEST_DB_URL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate_sqlite_to_mssql as migrate  # noqa: E402
import recognize  # noqa: E402

server = pytest.mark.skipif(not TEST_DB_URL, reason="needs GATESENSE_TEST_DB_URL (a SQL Server test database)")

# --- The setting -------------------------------------------------------------------------------------------------


def test_the_old_variable_alone_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", None)
    monkeypatch.setattr(config, "OLD_DATABASE_URL", "mssql+pyodbc://u:secret@host/GateSense")
    with pytest.raises(dbconn.DatabaseConfigError, match="GATESENSE_DB_URL"):
        db.connect()
    problems = startup_problems()
    assert any("GATESENSE_DATABASE_URL is set" in p for p in problems)
    assert not any("secret" in p for p in problems)


def test_urls_that_cant_be_used(monkeypatch):
    for url, message in (("postgresql://h/g", "PostgreSQL is not supported yet"),
                         ("mssql+pymssql://h/g", "mssql\\+pyodbc"), ("oracle://h/g", "unknown database type")):
        monkeypatch.setattr(config, "DATABASE_URL", url)
        with pytest.raises(dbconn.DatabaseConfigError, match=message):
            db.connect()


def test_the_password_is_never_shown():
    url = r"mssql+pyodbc://gatesense_app:p%40ss:w0rd@localhost\SQLEXPRESS01/GateSense?driver=ODBC+Driver+18+for+SQL+Server"
    shown = dbconn.safe_url(url)
    assert "w0rd" not in shown and "p%40ss" not in shown and "***" in shown and "GateSense" in shown


def test_values_come_back_as_sqlite_returns_them():
    from app.mssql import Cursor, sqlite_value

    assert sqlite_value(datetime(2026, 10, 29, 22, 5, 0)) == "2026-10-29 22:05:00"
    assert sqlite_value(datetime(2026, 10, 29, 22, 5, 0, 123000)) == "2026-10-29 22:05:00.123"
    assert sqlite_value(date(2026, 10, 29)) == "2026-10-29"
    assert sqlite_value(time(6, 30)) == "06:30"
    assert sqlite_value(True) == 1 and sqlite_value(False) == 0
    assert sqlite_value(Decimal(7)) == 7 and sqlite_value(0.25) == 0.25
    rows = Cursor([("id",), ("Name",)], [(1, "a"), (2, "b")])
    first = rows.fetchone()
    assert first[0] == 1 and first["Name"] == "a" and first["name"] == "a" and dict(first) == {"id": 1, "Name": "a"}
    assert [tuple(r) for r in rows] == [(2, "b")]


def sqlite_schema(conn) -> dict:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    out = {}
    for t in sorted(tables):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        notnull = {r[1] for r in conn.execute(f"PRAGMA table_info({t})") if r[3] or r[5]}
        unique = set()
        for idx in conn.execute(f"PRAGMA index_list({t})"):
            if idx[2]:  # unique (includes the primary key of a WITHOUT-ROWID-less table's composite key)
                unique.add(tuple(r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})")))
        pk = tuple(r[1] for r in sorted(conn.execute(f"PRAGMA table_info({t})"), key=lambda r: r[5]) if r[5])
        if len(pk) > 1 or (pk and pk != ("id",)):
            unique.add(pk)
        indexes = {tuple(r[2] for r in conn.execute(f"PRAGMA index_info({i[1]})"))
                   for i in conn.execute(f"PRAGMA index_list({t})") if i[3] == "c" and not i[2]}
        out[t] = {"columns": cols, "notnull": notnull, "unique": unique, "indexes": indexes}
    return out


def test_the_sql_server_schema_is_sqlites(tmp_path):
    from app import mssql

    assert mssql.SCHEMA_VERSION == len(db.VERSIONS)  # a new schema step needs its SQL Server version too
    with closing(db.connect(tmp_path / "a.db")) as conn:
        sqlite = sqlite_schema(conn)
    assert sorted(sqlite) == sorted(mssql.TABLES)
    for name in mssql.TABLES:
        table = mssql.metadata.tables[name]
        unique = {tuple(c.name for c in u.columns) for u in table.constraints
                  if u.__class__.__name__ == "UniqueConstraint"}
        unique |= {tuple(c.name for c in i.columns) for i in table.indexes if i.unique}
        pk = tuple(c.name for c in table.primary_key.columns)
        if pk != ("id",):
            unique.add(pk)
        expected = {"columns": [c.name for c in table.columns],
                    "notnull": {c.name for c in table.columns if not c.nullable},
                    "unique": unique,
                    "indexes": {tuple(c.name for c in i.columns) for i in table.indexes if not i.unique}}
        assert expected == sqlite[name], name


# --- The same results on both ------------------------------------------------------------------------------------

MON = date(2026, 10, 19)
NOW = datetime(2026, 10, 22, 18, 0)


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm))


def scenario(conn) -> dict:
    """Night shift across midnight, a late IN, an early leave, repeats, a camera pass in the cool-down, corrections
    (manual event, re-time, void, restore), an effective-dated shift change, a renamed shift; then everything the
    screens, reports and today.py read, under both day rules."""
    db.sync_persons(conn, ["1001", "1002", "1003"])
    entrance = db.upsert_camera(conn, "entrance", 0, "IN")
    exit_ = db.upsert_camera(conn, "exit", "rtsp://${CAM_USER}:${CAM_PASS}@10.0.0.5/1", "OUT")
    ids = db.person_ids(conn)
    shifts = {s.name: s for s in db.list_shifts(conn, MON)}
    db.set_roster(conn, [(ids["1001"], MON, shifts["Night"].id), (ids["1001"], MON + timedelta(1), shifts["Night"].id),
                         (ids["1002"], MON, shifts["General"].id), (ids["1002"], MON + timedelta(1), None),
                         (ids["1003"], MON, shifts["Morning"].id)], "week plan", "hr1")
    passes = [("1001", entrance, at(MON, "21:40")), ("1001", entrance, at(MON, "21:43")),  # cool-down
              ("1001", exit_, at(MON + timedelta(1), "06:05")), ("1001", entrance, at(MON + timedelta(1), "22:31")),
              ("1001", exit_, at(MON + timedelta(2), "05:10")),  # early leave
              ("1002", entrance, at(MON, "08:42")), ("1002", exit_, at(MON, "17:15")),
              ("1002", entrance, at(MON, "12:00")),  # a repeat
              ("1002", entrance, at(MON + timedelta(1), "09:00")),  # an OFF day with a pass
              ("1003", exit_, at(MON, "14:40"))]  # missing IN
    results = [db.add_camera_event(conn, ids[c], cam, t, 0.61234567, f"snapshots/{t:%Y-%m-%d}/{c}.jpg", 300)
               for c, cam, t in passes]
    manual = db.add_manual_event(conn, ids["1003"], entrance, at(MON, "06:35"), "forgot to look up", "hr1")
    db.edit_event_time(conn, results[5][0], at(MON, "08:40"), "camera clock", "hr1")
    db.set_event_voided(conn, results[7][0], True, "double", "hr1")
    db.set_event_voided(conn, results[7][0], False, "not double", "hr1")
    db.change_shift(conn, shifts["General"].id, Shift("General", time(8, 30), time(17, 0), False),
                    MON + timedelta(1), "new hours", "admin")
    db.rename_shift(conn, shifts["Morning"].id, "Early", "clearer", "admin")
    db.update_person(conn, ids["1002"], "Bilal Khan", "Office", "Day", "typo", "hr1")
    first, last = MON - timedelta(1), MON + timedelta(3)
    out = {"results": results, "manual": manual}
    for mode in ("on", "off"):
        db.set_shifts_enabled(conn, mode == "on", "switch", "admin")
        out[f"days {mode}"] = db.attendance_days(conn, first, last, NOW)
        out[f"repeats {mode}"] = sorted(db.attendance_repeat_event_ids(conn, first, last))
        out[f"report {mode}"] = reports.range_rows(conn, first, last)
    out["events"] = [dict(r) for r in db.recent_events(conn, 100)]
    out["events on"] = [dict(r) for r in db.events_on(conn, MON)]
    out["last times"] = db.last_event_times(conn, at(MON, "00:00"))
    out["roster"] = [dict(r) for r in db.roster_rows(conn, first, last)]
    out["versions"] = {k: v for k, v in db.shift_versions(conn).items()}
    out["persons"] = [dict(r) for r in db.list_persons(conn)]
    out["cameras"] = [dict(r) for r in db.list_cameras(conn)]
    out["audit"] = [{k: v for k, v in dict(r).items() if k != "timestamp"} for r in db.audit_entries(conn)]
    out["mode"] = {k: v for k, v in db.shift_mode(conn).items() if k != "updated"}
    return out


@server
def test_sql_server_gives_the_same_results_as_sqlite(tmp_path):
    with closing(db.connect(tmp_path / "a.db")) as conn:
        expected = scenario(conn)
    with closing(db.connect()) as conn:
        assert dbconn.is_mssql(conn)
        actual = scenario(conn)
    assert expected["days on"] and expected["report off"]  # the scenario really produces days
    for key in expected:
        assert actual[key] == expected[key], key


# --- Transactions, locks, connections ----------------------------------------------------------------------------


@server
def test_one_writer_at_a_time_and_readers_never_wait(monkeypatch):
    monkeypatch.setattr(config, "DB_LOCK_TIMEOUT_MS", 300)
    with closing(db.connect()) as a, closing(db.connect()) as b:
        db.sync_persons(a, ["1001"])
        a.execute("BEGIN IMMEDIATE")
        a.execute("UPDATE persons SET name = 'changing' WHERE employee_code = '1001'")
        assert b.execute("SELECT name FROM persons").fetchone()[0] == "1001"  # the last committed value, at once
        with pytest.raises(dbconn.ERRORS, match="database is locked"):
            b.execute("UPDATE persons SET name = 'other'")
        assert not b.in_transaction
        a.commit()
        with b:
            b.execute("UPDATE persons SET name = 'other'")
        assert a.execute("SELECT name FROM persons").fetchone()[0] == "other"


@server
def test_a_failed_transaction_leaves_nothing_and_releases_the_lock(monkeypatch):
    monkeypatch.setattr(config, "DB_LOCK_TIMEOUT_MS", 300)
    with closing(db.connect()) as conn:
        with pytest.raises(ValueError):
            db.add_person(conn, "1001", "", None, None, "new", "hr1")  # refused before writing
        with pytest.raises(dbconn.INTEGRITY_ERRORS):
            with conn:
                conn.execute("INSERT INTO sites (name) VALUES ('Second')")
                conn.execute("INSERT INTO sites (name) VALUES ('Second')")
        assert conn.execute("SELECT count(*) FROM sites").fetchone()[0] == 1
    with closing(db.connect()) as other:
        with other:
            other.execute("INSERT INTO sites (name) VALUES ('Third')")  # the lock was released


@pytest.fixture
def no_driver_retry(monkeypatch):
    """ODBC Driver 18 quietly reconnects an idle connection that was cut (ConnectRetryCount, default 1): off here,
    so these tests see GateSense's own reconnecting."""
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB_URL + "&ConnectRetryCount=0")
    monkeypatch.setattr(config, "DB_RECONNECT_SECONDS", 0)


def kill(spid: int) -> None:
    from app import mssql

    with closing(mssql.Connection(TEST_DB_URL)) as admin:
        admin.execute(f"KILL {int(spid)}")


@server
def test_a_lost_connection_is_replaced(no_driver_retry):
    conn = db.connect()
    try:
        kill(conn.execute("SELECT @@SPID").fetchone()[0])  # like a SQL Server restart, for this connection
        with pytest.raises(dbconn.ERRORS):
            conn.execute("SELECT count(*) FROM persons")
        assert conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 0  # a new connection
    finally:
        conn.close()


@server
def test_a_lost_connection_waits_before_trying_again(no_driver_retry, monkeypatch):
    monkeypatch.setattr(config, "DB_RECONNECT_SECONDS", 60)
    with closing(db.connect()) as conn:
        kill(conn.execute("SELECT @@SPID").fetchone()[0])
        with pytest.raises(dbconn.ERRORS):
            conn.execute("SELECT 1")
        with pytest.raises(dbconn.ERRORS, match="trying again in"):
            conn.execute("SELECT 1")  # no new connection attempt yet (the camera loop isn't held up)


def track(name: str = "1001") -> TrackState:
    t = TrackState(7)
    for i in range(3):
        t.add(Sample(0.9, Match(name, name, 0.8 + i / 100, None, None, "match"), np.full((20, 20, 3), 99, np.uint8)))
    return t


def camera_events() -> list[dict]:
    with closing(db.connect()) as conn:
        return [dict(r) for r in db.recent_events(conn)]


@server
def test_an_event_whose_commit_answer_was_lost_is_recorded_once(monkeypatch):
    """The worker's COMMIT reached the server but the answer didn't (connection lost): the event stays pending, the
    retry finds it in the database, and it is saved once, with its snapshot."""
    import pyodbc

    from app import mssql

    with closing(db.connect()) as conn:
        db.sync_persons(conn, ["1001"])
    rec = recognize.Recorder({"name": "gate", "direction": "IN"}, 0, use_db=True)
    real_commit, lost = mssql.Connection.commit, []

    def commit_then_lose_the_answer(self):
        real_commit(self)
        if self is rec.conn and not lost:
            lost.append(True)
            raise pyodbc.OperationalError("08S01", "Communication link failure")

    monkeypatch.setattr(mssql.Connection, "commit", commit_then_lose_the_answer)
    when = datetime.now().replace(microsecond=0)
    kind, detail = rec.record(f"{when:%H:%M:%S}", when, track(), "1001", 3)
    assert kind == "event" and "waiting for the database" in detail and len(rec.pending) == 1
    assert len(camera_events()) == 1  # it did reach the database
    rec.flush()
    assert not rec.pending
    saved = camera_events()
    assert len(saved) == 1 and saved[0]["snapshot_path"] and (config.DATA_DIR / saved[0]["snapshot_path"]).is_file()
    rec.conn.close()


@server
def test_events_wait_while_sql_server_is_down_then_are_written_once(no_driver_retry, monkeypatch):
    with closing(db.connect()) as conn:
        db.sync_persons(conn, ["1001"])
    rec = recognize.Recorder({"name": "gate", "direction": "IN"}, 0, use_db=True)
    rec.cooldown.seconds = 0
    monkeypatch.setattr(config, "COOLDOWN_SECONDS", 0)
    kill(rec.conn.execute("SELECT @@SPID").fetchone()[0])
    t0 = datetime(2026, 10, 23, 9)
    assert rec.record("09:00:00", t0, track(), "1001", 3)[1].startswith("IN waiting for the database")
    rec.record("09:00:05", t0 + timedelta(seconds=5), track(), "1001", 3)
    rec.flush()
    assert not rec.pending
    assert [e["timestamp"] for e in reversed(camera_events())] == ["2026-10-23 09:00:00", "2026-10-23 09:00:05"]
    rec.conn.close()


# --- Backups ------------------------------------------------------------------------------------------------------


@pytest.fixture
def backup_folder(tmp_path):
    """SQL Server's service writes backup files itself, so the folder must be one it can write:
    GATESENSE_TEST_BACKUP_DIR (prepared by hand; the tests never change folder permissions), else skipped."""
    import os
    import shutil
    import uuid

    base = os.environ.get("GATESENSE_TEST_BACKUP_DIR")
    if not base:
        pytest.skip("needs GATESENSE_TEST_BACKUP_DIR: a folder the SQL Server service can write")
    folder = Path(base) / f"gatesense-test-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@server
def test_backup_sets_on_sql_server(tmp_path, backup_folder):
    with closing(db.connect()) as conn:
        db.sync_persons(conn, ["1001"])
    copy_dir = tmp_path / "usb"
    made = maintenance.make_backup(Database(), maintenance.MANUAL, folder=backup_folder, copy_dir=copy_dir)
    assert made.database.suffix == ".bak" and made.database.name.startswith("GateSense_Test-manual-")
    assert maintenance.integrity(made.database) == "ok" and made.copy_error is None
    assert (copy_dir / made.database.name).read_bytes() == made.database.read_bytes()
    assert [s.stamp for s in maintenance.backup_sets(backup_folder)] == [made.stamp]
    made.database.write_bytes(made.database.read_bytes()[:4096])  # cut short
    assert maintenance.integrity(made.database) != "ok"


@server
def test_restore_prints_the_commands_for_a_sql_server_backup(backup_folder, capsys):
    import restore

    made = maintenance.make_backup(Database(), maintenance.MANUAL, folder=backup_folder, copy_dir=False)
    assert restore.main(["latest", "--yes", "--from", str(backup_folder)]) == 0
    out = capsys.readouterr().out
    assert "RESTORE DATABASE [GateSense_Test] FROM DISK" in out and str(made.database.resolve()) in out
    assert "--only" not in out  # the set has no other parts to restore
    with pytest.raises(maintenance.BackupError, match="administrator"):
        maintenance.restore_set(made, ("database",))


# --- The migration script ----------------------------------------------------------------------------------------


def sqlite_file(tmp_path) -> Path:
    path = tmp_path / "old" / "attendance.db"
    path.parent.mkdir()
    with closing(db.connect(path)) as conn:
        scenario(conn)
        conn.execute("INSERT INTO users (username, full_name, role, password_hash, created_at, must_change_password) "
                     "VALUES ('ayşe', 'Ayşe Öztürk 名前', 'admin', 'hash', '2026-10-01 08:00:00', 1)")
        conn.execute("INSERT INTO user_preferences (user_id, \"key\", value, updated) VALUES (1, 'enroll_source', "
                     "'webcam', '2026-10-02T09:15:00')")  # as Database.set_preference writes it (with a T)
        conn.commit()
    return path


def rows_of(conn, table: str) -> list[tuple]:
    from app import mssql

    cols = ", ".join(f'"{c.name}"' for c in mssql.metadata.tables[table].columns)
    return sorted(tuple(r) for r in conn.execute(f"SELECT {cols} FROM {table}"))


@server
def test_migration_copies_everything_with_its_ids(tmp_path, capsys):
    from app import mssql

    path = sqlite_file(tmp_path)
    before = path.read_bytes()
    Gallery.from_people({"1001": [np.ones(512, np.float32) / np.sqrt(512)]}).save()
    gallery = config.GALLERY_PATH.read_bytes()
    assert migrate.main(["--sqlite", str(path)]) == 0
    out = capsys.readouterr().out
    assert "match" in out and "DIFFERENT" not in out and "untouched" in out and "SQLite file unchanged: yes" in out
    assert "user_preferences.updated: 1 value(s)" in out  # the 'T' form, noted
    assert path.read_bytes() == before and config.GALLERY_PATH.read_bytes() == gallery
    with closing(db.connect(path)) as old, closing(db.connect()) as new:
        for table in mssql.TABLES:
            want = rows_of(old, table)
            if table == "user_preferences":
                want = [r[:3] + (r[3].replace("T", " "),) for r in want]
            assert rows_of(new, table) == want, table
        assert db.attendance_days(new, MON, MON + timedelta(2), NOW) == db.attendance_days(old, MON, MON + timedelta(2), NOW)
        # New rows continue after the copied ids, as in SQLite.
        last = old.execute("SELECT MAX(id) FROM attendance_events").fetchone()[0]
        assert db.add_manual_event(new, 1, 1, at(MON, "07:00"), "test", "hr1") == last + 1
    assert migrate.main(["--sqlite", str(path), "--check"]) == 1  # the new event is only in SQL Server
    assert "differs" in capsys.readouterr().out


@server
def test_migration_refuses_a_filled_database_unless_forced(tmp_path, capsys):
    path = sqlite_file(tmp_path)
    with closing(db.connect()) as conn:  # GateSense started on SQL Server first: default site and shifts
        db.sync_persons(conn, ["9999"])
    assert migrate.main(["--sqlite", str(path)]) == 1
    assert "already have rows" in capsys.readouterr().out
    with closing(db.connect()) as conn:
        assert [r[0] for r in conn.execute("SELECT employee_code FROM persons")] == ["9999"]  # nothing changed
    assert migrate.main(["--sqlite", str(path), "--force"]) == 0
    assert "--force: emptied" in capsys.readouterr().out
    with closing(db.connect()) as conn:
        assert sorted(r[0] for r in conn.execute("SELECT employee_code FROM persons")) == ["1001", "1002", "1003"]
    assert migrate.main(["--sqlite", str(path), "--check"]) == 0


@server
def test_migration_refuses_values_it_would_change(tmp_path, capsys):
    path = sqlite_file(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE attendance_events SET timestamp = '2026-10-19 21:40:00+05:00' WHERE id = 1")
        conn.execute("UPDATE persons SET employee_code = '1001 ' WHERE employee_code = '1002'")
        conn.commit()
    assert migrate.main(["--sqlite", str(path)]) == 1
    out = capsys.readouterr().out
    assert "has a time zone" in out and "would be the same key" in out and "Nothing was written" in out
    with closing(db.connect(migrate=False)) as conn:
        from app import mssql

        assert mssql.schema_version(conn) is None  # not even the tables


@server
def test_migration_rolls_back_everything_on_a_difference(tmp_path, monkeypatch, capsys):
    from app import mssql

    path = sqlite_file(tmp_path)

    def differ(expected, actual):
        raise migrate.Refused("a row differs after the copy")

    monkeypatch.setattr(migrate, "compare", differ)
    assert migrate.main(["--sqlite", str(path)]) == 1
    assert "Nothing was written" in capsys.readouterr().out
    with closing(db.connect(migrate=False)) as conn:
        assert mssql.schema_version(conn) is None and mssql.existing_tables(conn) == []


@server
def test_migration_refuses_while_gatesense_runs(tmp_path, monkeypatch, capsys):
    path = sqlite_file(tmp_path)
    monkeypatch.setattr(migrate, "api_pid", lambda: 4242)
    assert migrate.main(["--sqlite", str(path)]) == 1
    assert "GateSense is running" in capsys.readouterr().out


def test_migration_needs_the_sql_server_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATABASE_URL", None)
    assert migrate.main(["--sqlite", str(tmp_path / "a.db")]) == 1
    assert "set GATESENSE_DB_URL" in capsys.readouterr().out
