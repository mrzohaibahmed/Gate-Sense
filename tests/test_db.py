import json
from datetime import date, datetime

import pytest

from app import db
from app.attendance import COMPLETE, MISSING_EXIT


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "attendance.db")
    yield c
    c.close()


def test_tables_created(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"persons", "cameras", "attendance_events"} <= names
    assert "daily_attendance" not in names  # removed in schema step 4: summaries are computed from the events


def test_sync_persons_deactivates_removed(conn):
    db.sync_persons(conn, ["ali", "waqas"])
    db.sync_persons(conn, ["waqas"], removed=["ali"])
    assert list(db.person_ids(conn)) == ["waqas"]


def test_sync_persons_leaves_people_without_photos_alone(conn):
    db.add_person(conn, "new1", "New Person", None, None, "added on enrollment page", "admin")
    db.sync_persons(conn, ["waqas"])  # new1 has no photos yet, so isn't in the gallery
    assert set(db.person_ids(conn)) == {"new1", "waqas"}


def test_add_person(conn):
    db.add_person(conn, "e042", "Ayesha Khan", "HR", "day", "new hire", "admin")
    [p] = db.list_persons(conn)
    assert (p["employee_code"], p["name"], p["department"], p["shift"], p["active"]) == \
        ("e042", "Ayesha Khan", "HR", "day", 1)
    assert db.audit_entries(conn)[0]["action"] == "add_person"
    with pytest.raises(ValueError, match="already exists"):
        db.add_person(conn, "e042", "Someone Else", None, None, "dup", "admin")


def test_sync_persons_keeps_admin_deactivation(conn):
    db.sync_persons(conn, ["waqas"])
    pid = db.person_ids(conn)["waqas"]
    db.set_person_active(conn, pid, False, "left the company", "admin")
    db.sync_persons(conn, ["waqas"])  # re-running enroll must not re-activate
    assert db.person_ids(conn) == {}
    assert db.active_person_id(conn, "waqas") is None


def test_old_database_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE attendance_events (id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, "
        "camera_id INTEGER NOT NULL, timestamp TEXT NOT NULL, match_score REAL NOT NULL, snapshot_path TEXT)"
    )
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(attendance_events)")}
    assert {"manual", "voided"} <= cols
    conn.close()


@pytest.fixture
def setup(conn):
    db.sync_persons(conn, ["waqas"])
    pid = db.person_ids(conn)["waqas"]
    cam_in = db.upsert_camera(conn, "entrance", 0, "IN")
    cam_out = db.upsert_camera(conn, "exit", 1, "OUT")
    return conn, pid, cam_in, cam_out


def audit(conn):
    return [dict(r) for r in db.audit_entries(conn)]


def test_reason_is_required(setup):
    conn, pid, cam_in, _ = setup
    for reason in ["", "   "]:
        with pytest.raises(ValueError):
            db.add_manual_event(conn, pid, cam_in, datetime(2026, 10, 20, 9, 0), reason, "admin")
    assert conn.execute("SELECT COUNT(*) FROM attendance_events").fetchone()[0] == 0
    assert audit(conn) == []


def test_manual_event_fixes_missing_exit(setup):
    conn, pid, cam_in, cam_out = setup
    day = date(2026, 10, 20)
    db.add_event(conn, pid, cam_in, datetime(2026, 10, 20, 9, 0), 0.8, None)
    assert db.day_summaries(conn, day)[0][0].status == MISSING_EXIT

    eid = db.add_manual_event(conn, pid, cam_out, datetime(2026, 10, 20, 17, 0), "forgot exit camera", "admin")
    s = db.day_summaries(conn, day)[0][0]
    assert s.status == COMPLETE and s.worked_hours == 8.0
    row = conn.execute("SELECT manual, match_score FROM attendance_events WHERE id = ?", (eid,)).fetchone()
    assert row["manual"] == 1 and row["match_score"] == 0
    [entry] = audit(conn)
    assert entry["action"] == "add_event" and entry["reason"] == "forgot exit camera"
    assert entry["target"] == f"event:{eid}" and entry["user"] == "admin"


def test_void_and_restore_event(setup):
    conn, pid, cam_in, cam_out = setup
    day = date(2026, 10, 20)
    db.add_event(conn, pid, cam_in, datetime(2026, 10, 20, 9, 0), 0.8, None)
    wrong = db.add_event(conn, pid, cam_out, datetime(2026, 10, 20, 9, 1), 0.5, None)
    db.set_event_voided(conn, wrong, True, "was someone else", "admin")
    assert db.day_summaries(conn, day)[0][0].status == MISSING_EXIT
    assert len(db.events_on(conn, day)) == 2  # still listed for the corrections page
    db.set_event_voided(conn, wrong, False, "it was him after all", "admin")
    assert db.day_summaries(conn, day)[0][0].status == COMPLETE
    assert [e["action"] for e in audit(conn)] == ["restore_event", "void_event"]


def test_edit_event_time_logs_old_and_new(setup):
    conn, pid, cam_in, _ = setup
    eid = db.add_event(conn, pid, cam_in, datetime(2026, 10, 20, 9, 30), 0.8, None)
    db.edit_event_time(conn, eid, datetime(2026, 10, 20, 9, 0), "camera clock was off", "admin")
    assert db.events_on(conn, date(2026, 10, 20))[0]["timestamp"] == "2026-10-20 09:00:00"
    details = json.loads(audit(conn)[0]["details"])
    assert details["timestamp"] == ["2026-10-20 09:30:00", "2026-10-20 09:00:00"]


def test_update_person_logs_only_changes(setup):
    conn, pid, _, _ = setup
    db.update_person(conn, pid, "Waqas Saeed", "IT", "", "real name", "admin")
    db.update_person(conn, pid, "Waqas Saeed", "IT", None, "no change", "admin")  # nothing changed: no audit row
    [entry] = audit(conn)
    assert json.loads(entry["details"]) == {"name": ["waqas", "Waqas Saeed"], "department": [None, "IT"]}
    with pytest.raises(ValueError):
        db.update_person(conn, pid, "  ", None, None, "blank name", "admin")


def test_voided_events_do_not_seed_cooldown(setup):
    conn, pid, cam_in, _ = setup
    eid = db.add_event(conn, pid, cam_in, datetime(2026, 10, 20, 9, 0), 0.8, None)
    db.set_event_voided(conn, eid, True, "wrong person", "admin")
    assert db.last_event_times(conn, datetime(2026, 10, 20, 8, 55)) == {}


def test_camera_passwords_never_stored(conn):
    url = "rtsp://admin:S3cretPw@192.168.1.64:554/Streaming/Channels/101"
    db.upsert_camera(conn, "gate", url, "IN")
    db.ensure_cameras(conn, [{"name": "gate2", "source": url, "direction": "OUT"}])
    sources = [c["source"] for c in db.list_cameras(conn)]
    assert sources == ["rtsp://admin:***@192.168.1.64:554/Streaming/Channels/101"] * 2


def test_ensure_cameras_keeps_existing_source(conn):
    db.upsert_camera(conn, "exit", "clip.mp4", "OUT")
    db.ensure_cameras(conn, [{"name": "exit", "source": 1, "direction": "OUT"},
                             {"name": "entrance", "source": 0, "direction": "IN"}])
    cams = {c["name"]: c["source"] for c in db.list_cameras(conn)}
    assert cams == {"entrance": "0", "exit": "clip.mp4"}


def test_events_to_day_summaries(conn):
    db.sync_persons(conn, ["ali", "waqas"])
    ids = db.person_ids(conn)
    cam_in = db.upsert_camera(conn, "entrance", 0, "IN")
    cam_out = db.upsert_camera(conn, "exit", "clip.mp4", "OUT")
    day = date(2026, 10, 20)
    db.add_event(conn, ids["waqas"], cam_in, datetime(2026, 10, 20, 9, 0), 0.8, None)
    db.add_event(conn, ids["waqas"], cam_out, datetime(2026, 10, 20, 17, 30), 0.7, None)
    db.add_event(conn, ids["ali"], cam_in, datetime(2026, 10, 20, 9, 15), 0.6, None)
    db.add_event(conn, ids["ali"], cam_out, datetime(2026, 10, 19, 18, 0), 0.6, None)  # yesterday: ignored

    summaries = {s.person: s for s in db.day_summaries(conn, day)[0]}
    assert summaries["waqas"].status == COMPLETE and summaries["waqas"].worked_hours == 8.5
    assert summaries["waqas"].first_in == datetime(2026, 10, 20, 9, 0)
    assert summaries["ali"].status == MISSING_EXIT


def test_last_event_times_per_direction_since_a_time(conn):
    """Seeds a worker's cool-down: the latest event per direction from `since` on, across midnight."""
    db.sync_persons(conn, ["waqas"])
    pid = db.person_ids(conn)["waqas"]
    gate1, gate2 = db.upsert_camera(conn, "gate1", 0, "IN"), db.upsert_camera(conn, "gate2", 1, "IN")
    out = db.upsert_camera(conn, "exit", 2, "OUT")
    db.add_event(conn, pid, gate1, datetime(2026, 10, 20, 23, 50), 0.8, None)
    db.add_event(conn, pid, gate2, datetime(2026, 10, 20, 23, 57), 0.8, None)
    db.add_event(conn, pid, out, datetime(2026, 10, 21, 0, 1), 0.8, None)
    db.add_event(conn, pid, gate1, datetime(2026, 10, 20, 9, 0), 0.8, None)  # before `since`
    assert db.last_event_times(conn, datetime(2026, 10, 20, 23, 55)) == {
        ("waqas", "IN"): datetime(2026, 10, 20, 23, 57), ("waqas", "OUT"): datetime(2026, 10, 21, 0, 1)}
    assert db.last_event_times(conn, datetime(2026, 10, 21, 0, 2)) == {}


@pytest.fixture
def gates(conn):
    db.sync_persons(conn, ["waqas"])
    pid = db.person_ids(conn)["waqas"]
    return (pid, db.upsert_camera(conn, "gate1", 0, "IN"), db.upsert_camera(conn, "gate2", 1, "IN"),
            db.upsert_camera(conn, "exit", 2, "OUT"))


def test_camera_event_cooldown_across_cameras_of_one_direction(conn, gates):
    pid, gate1, gate2, out = gates
    t = datetime(2026, 10, 20, 8, 0)
    first, previous = db.add_camera_event(conn, pid, gate1, t, 0.8, None, 300)
    assert first and previous is None
    assert db.add_camera_event(conn, pid, gate2, t.replace(second=20), 0.8, None, 300) == (None, t)  # other IN camera
    assert db.add_camera_event(conn, pid, out, t.replace(minute=1), 0.8, None, 300)[0]  # OUT: another direction
    later, previous = db.add_camera_event(conn, pid, gate2, t.replace(minute=5), 0.8, None, 300)
    assert later and previous == t  # 5 minutes after the recorded IN
    assert conn.execute("SELECT count(*) FROM attendance_events").fetchone()[0] == 3


def test_camera_event_cooldown_runs_across_midnight_and_ignores_voided(conn, gates):
    pid, gate1, gate2, _ = gates
    late = datetime(2026, 10, 20, 23, 58)
    first = db.add_camera_event(conn, pid, gate1, late, 0.8, None, 300)[0]
    assert db.add_camera_event(conn, pid, gate2, datetime(2026, 10, 21, 0, 2), 0.8, None, 300) == (None, late)
    assert db.add_camera_event(conn, pid, gate2, datetime(2026, 10, 21, 0, 3), 0.8, None, 300)[0]  # 5 min later
    db.set_event_voided(conn, first, True, "someone else", "admin")
    assert db.add_camera_event(conn, pid, gate2, datetime(2026, 10, 20, 23, 59), 0.8, None, 300)[0]


def test_two_workers_seeing_one_person_at_once_record_one_event(tmp_path):
    """Two worker processes (two IN cameras) write at the same moment: BEGIN IMMEDIATE makes the second one see the
    first one's event."""
    import threading

    path = tmp_path / "race.db"
    setup = db.connect(path)
    db.sync_persons(setup, ["waqas"])
    pid = db.person_ids(setup)["waqas"]
    cams = [db.upsert_camera(setup, f"gate{i}", i, "IN") for i in range(4)]
    setup.close()
    results, barrier = [], threading.Barrier(len(cams))

    def worker(cam):
        c = db.connect(path)
        barrier.wait()
        results.append(db.add_camera_event(c, pid, cam, datetime(2026, 10, 20, 8, 0, 1), 0.8, None, 300)[0])
        c.close()

    threads = [threading.Thread(target=worker, args=(cam,)) for cam in cams]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
