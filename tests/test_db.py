from datetime import date, datetime

import pytest

from app import db
from app.attendance import MISSING_EXIT, PRESENT


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "attendance.db")
    yield c
    c.close()


def test_tables_created(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"persons", "cameras", "attendance_events", "daily_attendance"} <= names


def test_sync_persons_deactivates_removed(conn):
    db.sync_persons(conn, ["ali", "waqas"])
    db.sync_persons(conn, ["waqas"])
    assert list(db.person_ids(conn)) == ["waqas"]


def test_events_to_daily_attendance(conn):
    db.sync_persons(conn, ["ali", "waqas"])
    ids = db.person_ids(conn)
    cam_in = db.upsert_camera(conn, "entrance", 0, "IN")
    cam_out = db.upsert_camera(conn, "exit", "clip.mp4", "OUT")
    day = date(2026, 10, 20)
    db.add_event(conn, ids["waqas"], cam_in, datetime(2026, 10, 20, 9, 0), 0.8, None)
    db.add_event(conn, ids["waqas"], cam_out, datetime(2026, 10, 20, 17, 30), 0.7, None)
    db.add_event(conn, ids["ali"], cam_in, datetime(2026, 10, 20, 9, 15), 0.6, None)
    db.add_event(conn, ids["ali"], cam_out, datetime(2026, 10, 19, 18, 0), 0.6, None)  # yesterday: ignored

    summaries = {s.person: s for s in db.refresh_daily(conn, day)}
    assert summaries["waqas"].status == PRESENT
    assert summaries["waqas"].worked_hours == 8.5
    assert summaries["ali"].status == MISSING_EXIT

    row = conn.execute(
        "SELECT * FROM daily_attendance WHERE person_id = ? AND date = ?", (ids["waqas"], "2026-10-20")
    ).fetchone()
    assert row["first_in"] == "2026-10-20 09:00:00" and row["last_out"] == "2026-10-20 17:30:00"

    db.refresh_daily(conn, day)  # re-running updates rows instead of duplicating them
    assert conn.execute("SELECT COUNT(*) FROM daily_attendance").fetchone()[0] == 2


def test_last_event_times_per_camera(conn):
    db.sync_persons(conn, ["waqas"])
    cam = db.upsert_camera(conn, "entrance", 0, "IN")
    db.add_event(conn, db.person_ids(conn)["waqas"], cam, datetime(2026, 10, 20, 9, 0), 0.8, None)
    db.add_event(conn, db.person_ids(conn)["waqas"], cam, datetime(2026, 10, 20, 9, 7), 0.8, None)
    assert db.last_event_times(conn, "entrance") == {("waqas", "entrance"): datetime(2026, 10, 20, 9, 7)}
    assert db.last_event_times(conn, "exit") == {}
