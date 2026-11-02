"""SQL Server (app/mssql.py, app/dbconn.py): the setting (GATESENSE_DB_URL; missing, old name or another database
refused; the password never shown), values read back in plain form, one writer at a time, a lost connection replaced,
an event whose COMMIT answer was lost recorded once, an older schema upgraded, and real backups.
"""

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
from app import config, db, dbconn
from app.gallery import Match
from app.tracker import Sample, TrackState
from tests.conftest import TEST_DB_URL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import recognize  # noqa: E402

# --- The setting -------------------------------------------------------------------------------------------------


def test_the_old_variable_alone_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", None)
    monkeypatch.setattr(config, "OLD_DATABASE_URL", "mssql+pyodbc://u:secret@host/GateSense")
    with pytest.raises(dbconn.DatabaseConfigError, match="GATESENSE_DB_URL"):
        db.connect()
    problems = startup_problems()
    assert any("GATESENSE_DATABASE_URL is set" in p for p in problems)
    assert not any("secret" in p for p in problems)


def test_no_url_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", None)
    with pytest.raises(dbconn.DatabaseConfigError, match="GATESENSE_DB_URL is not set"):
        db.connect()
    assert any("GATESENSE_DB_URL is not set" in p for p in startup_problems())


def test_urls_that_cant_be_used(monkeypatch):
    for url, message in (("postgresql://h/g", "PostgreSQL is not supported yet"),
                         ("sqlite:///attendance.db", "unknown database type"),
                         ("mssql+pymssql://h/g", "mssql\\+pyodbc"), ("oracle://h/g", "unknown database type")):
        monkeypatch.setattr(config, "DATABASE_URL", url)
        with pytest.raises(dbconn.DatabaseConfigError, match=message):
            db.connect()


def test_the_password_is_never_shown():
    url = r"mssql+pyodbc://gatesense_app:p%40ss:w0rd@localhost\SQLEXPRESS01/GateSense?driver=ODBC+Driver+18+for+SQL+Server"
    shown = dbconn.safe_url(url)
    assert "w0rd" not in shown and "p%40ss" not in shown and "***" in shown and "GateSense" in shown


def test_values_come_back_in_plain_form():
    from app.mssql import Cursor, plain_value

    assert plain_value(datetime(2026, 10, 29, 22, 5, 0)) == "2026-10-29 22:05:00"
    assert plain_value(datetime(2026, 10, 29, 22, 5, 0, 123000)) == "2026-10-29 22:05:00.123"
    assert plain_value(date(2026, 10, 29)) == "2026-10-29"
    assert plain_value(time(6, 30)) == "06:30"
    assert plain_value(True) == 1 and plain_value(False) == 0
    assert plain_value(Decimal(7)) == 7 and plain_value(0.25) == 0.25
    rows = Cursor([("id",), ("Name",)], [(1, "a"), (2, "b")])
    first = rows.fetchone()
    assert first[0] == 1 and first["Name"] == "a" and first["name"] == "a" and dict(first) == {"id": 1, "Name": "a"}
    assert [tuple(r) for r in rows] == [(2, "b")]


def test_an_older_database_is_upgraded_and_keeps_its_rows(capsys):
    with closing(db.connect()) as conn:
        db.sync_persons(conn, ["1001"])
        conn.execute("DROP TABLE unknown_visits")
        conn.execute("UPDATE gatesense_schema SET version = 7")
        conn.commit()
    with closing(db.connect()) as conn:
        assert db.schema_version(conn) == 8 and list(db.person_ids(conn)) == ["1001"]
        assert conn.execute("SELECT count(*) FROM unknown_visits").fetchone()[0] == 0
    assert "upgraded from schema version 7 to 8" in capsys.readouterr().out


# --- Transactions, locks, connections ----------------------------------------------------------------------------


def test_one_writer_at_a_time_and_readers_never_wait(monkeypatch):
    monkeypatch.setattr(config, "DB_LOCK_TIMEOUT_MS", 300)
    with closing(db.connect()) as a, closing(db.connect()) as b:
        db.sync_persons(a, ["1001"])
        a.begin()
        a.execute("UPDATE persons SET name = 'changing' WHERE employee_code = '1001'")
        assert b.execute("SELECT name FROM persons").fetchone()[0] == "1001"  # the last committed value, at once
        with pytest.raises(dbconn.ERRORS, match="database is locked"):
            b.execute("UPDATE persons SET name = 'other'")
        assert not b.in_transaction
        a.commit()
        with b:
            b.execute("UPDATE persons SET name = 'other'")
        assert a.execute("SELECT name FROM persons").fetchone()[0] == "other"


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


def test_a_lost_connection_is_replaced(no_driver_retry):
    conn = db.connect()
    try:
        kill(conn.execute("SELECT @@SPID").fetchone()[0])  # like a SQL Server restart, for this connection
        with pytest.raises(dbconn.ERRORS):
            conn.execute("SELECT count(*) FROM persons")
        assert conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 0  # a new connection
    finally:
        conn.close()


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


def test_restore_prints_the_commands_for_a_sql_server_backup(backup_folder, capsys):
    import restore

    made = maintenance.make_backup(Database(), maintenance.MANUAL, folder=backup_folder, copy_dir=False)
    assert restore.main(["latest", "--yes", "--from", str(backup_folder)]) == 0
    out = capsys.readouterr().out
    assert "RESTORE DATABASE [GateSense_Test] FROM DISK" in out and str(made.database.resolve()) in out
    assert "--only" not in out  # the set has no other parts to restore
    with pytest.raises(maintenance.BackupError, match="administrator"):
        maintenance.restore_set(made, ("database",))
