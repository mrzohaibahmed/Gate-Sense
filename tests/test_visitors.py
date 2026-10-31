"""Unknown visitors (app/visitors.py, recognize.VisitorLog, /api/unknowns, /api/settings/unknowns).

The worker records a face that matches nobody once (while it is still in view), with a snapshot, only while the
visitor log is on; never a known face, an uncertain one (between UNKNOWN_MAX_SCORE and the threshold) or a spoof; a
stranger who comes back within UNKNOWN_MERGE_SECONDS is the same visit; a track recognised after all withdraws it;
writes the database refuses wait and are retried (never twice); a visit not written before the log is switched off is
dropped. The API lists, filters and reviews visits (audited, admins and guards only), serves snapshots only from their
folder, and the switch is admin-only and audited. New visits reach admins and guards over the WebSocket, nobody else.
The nightly job deletes old visits and their snapshots.
"""

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import maintenance
from api.main import create_app
from app import config, db, visitors, workers
from app.gallery import Gallery, Match
from app.live import LiveWriter
from app.tracker import Sample, TrackState
from tests.fakes import FakeDetector, FakeLiveness, photo
from tests.test_api import wait_for
from tests.test_data_api import as_role, first_password, items, worker_app  # noqa: F401  (fixtures)
from tests.test_pipeline import (
    FixedRecognizer, RecordingRecognizer, SmallFaceDetector, full_hd, recognize, unit,
)

T0 = datetime(2026, 10, 20, 9, 0)
STRANGER = 0.2 * unit(0) + np.sqrt(1 - 0.04) * unit(7)  # 0.2 against waqas, 0 against ali: nobody
OTHER_STRANGER = unit(9)
UNCERTAIN = 0.4 * unit(0) + np.sqrt(1 - 0.16) * unit(7)  # 0.40: unknown, but probably waqas at a bad angle
CAM = {"name": "gate", "direction": "IN"}


# --- The rules (app/visitors.py) ----------------------------------------------------------------------------------


def sample(name, score, embedding=None):
    return Sample(0.9, Match(name, name or "waqas", score, None, None, "match" if name else "below threshold"),
                  np.zeros((4, 4, 3), np.uint8), embedding)


def track(*samples, live=0, decided=None):
    t = TrackState(1)
    t.samples, t.live_frames, t.decided = list(samples), live, decided
    return t


def test_a_track_qualifies_only_when_every_kept_frame_matches_nobody_clearly():
    low = [sample(None, 0.2), sample(None, 0.1), sample(None, 0.3)]
    assert visitors.qualifies(track(*low), 3, 0.35, 0)
    assert not visitors.qualifies(track(*low[:2]), 3, 0.35, 0)  # not enough frames yet
    assert not visitors.qualifies(track(*low, sample(None, 0.40)), 3, 0.35, 0)  # one uncertain frame
    assert not visitors.qualifies(track(*low, sample("waqas", 0.6)), 3, 0.35, 0)  # one match
    assert not visitors.qualifies(track(*low, decided="waqas"), 3, 0.35, 0)
    assert not visitors.qualifies(track(*low, live=2), 3, 0.35, 3)  # liveness: 3 live frames needed
    assert visitors.qualifies(track(*low, live=3), 3, 0.35, 3)


def test_the_closest_person_and_the_track_embedding():
    a, b = unit(1) * 2, unit(2)
    t = track(sample(None, 0.2, a), sample(None, 0.3, b), sample(None, 0.1))
    assert visitors.closest(t.samples) == ("waqas", 0.3)
    emb = visitors.track_embedding(t.samples)
    assert np.isclose(np.linalg.norm(emb), 1) and np.isclose(emb[1], emb[2])  # each frame counts once, normalised
    assert visitors.track_embedding([sample(None, 0.1)]) is None


def test_stranger_memory_matches_recent_faces_only():
    memory = visitors.StrangerMemory(seconds=300, same_person=0.45)
    v = visitors.Visit(T0, T0, 0.2, "waqas", None, 100, None)
    memory.remember(unit(7), T0, v)
    assert memory.find(unit(7), T0 + timedelta(seconds=299)) is v
    assert memory.find(unit(9), T0 + timedelta(seconds=10)) is None  # another face
    assert memory.find(unit(7), T0 + timedelta(seconds=301)) is None and len(memory) == 0  # forgotten in time
    memory.remember(unit(7), T0, v)
    v.deleted = True
    assert memory.find(unit(7), T0) is None  # a withdrawn visit is never matched


# --- The worker ---------------------------------------------------------------------------------------------------


@pytest.fixture
def make_pipeline():
    """A pipeline with the real Recorder and VisitorLog on the test database; the visitor log on unless on=False."""
    recorders = []

    def make(on=True, liveness=None):
        conn = db.connect()
        db.sync_persons(conn, ["waqas", "ali"])
        if on:
            db.set_unknown_log_enabled(conn, True, "test", "admin")
        conn.close()
        recorder = recognize.Recorder(CAM, 0, use_db=True)
        recorders.append(recorder)
        log = recognize.VisitorLog(recorder, liveness is not None)
        gallery = Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]})
        return recognize.Pipeline(gallery, FakeDetector(), FixedRecognizer(STRANGER), recorder, LiveWriter("gate"),
                                  liveness=liveness, visitors=log)

    yield make
    for r in recorders:
        r.conn.close()


def frames(p, vector, n, start=0.0, kind="good"):
    """n frames from `start` seconds (video time from T0), 8 a second, the face `vector` (kind "none": no face)."""
    p.recognizer = FixedRecognizer(vector)
    for i in range(n):
        t = start + i * 0.125
        p.process(photo(kind, seed=i), t, T0 + timedelta(seconds=t), f"t={t:.2f}")


def leave(p, start):
    frames(p, None, 20, start=start, kind="none")  # 2.5 s without the face: the track ends


def visits() -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM unknown_visits ORDER BY id")]
    finally:
        conn.close()


def kinds(p) -> list[str]:
    return [d["kind"] for d in p.live.decisions]


def test_a_stranger_is_recorded_once_while_still_in_view(make_pipeline, capsys):
    p = make_pipeline()
    frames(p, STRANGER, 2)
    assert visits() == []  # two kept frames: not yet
    frames(p, STRANGER, 1, start=0.25)
    [v] = visits()  # the third: recorded at once, the person is still there
    assert (v["first_seen"], v["last_seen"], v["tracks"], v["status"]) == (
        "2026-10-20 09:00:00", "2026-10-20 09:00:00", 1, "new")
    assert (v["closest_code"], round(v["best_score"], 3), v["face_px"], v["liveness"]) == ("waqas", 0.2, 200, None)
    assert v["snapshot_path"].startswith("snapshots/unknown/2026-10-20/") and v["snapshot_path"].endswith("_gate.jpg")
    snap = cv2.imread(str(config.DATA_DIR / v["snapshot_path"]))
    assert snap is not None and max(snap.shape[:2]) <= config.UNKNOWN_SNAPSHOT_SIZE
    out = capsys.readouterr().out
    assert "Unknown visitor log ON" in out and "UNKNOWN VISITOR #1" in out

    frames(p, STRANGER, 17, start=0.375)  # still there until 2.375 s
    leave(p, start=2.5)
    [v] = visits()
    assert v["last_seen"] == "2026-10-20 09:00:02" and v["tracks"] == 1  # written when the track ended
    assert kinds(p).count("visitor") == 1 and p.stats["unknown visitor"] == 1
    visitor = next(d for d in p.live.decisions if d["kind"] == "visitor")
    assert visitor["detail"] == "unknown visitor recorded (closest waqas 0.20)" and visitor["face_px"] == 200


def test_nothing_is_recorded_while_the_log_is_off(make_pipeline):
    p = make_pipeline(on=False)
    frames(p, STRANGER, 10)
    assert visits() == [] and "visitor" not in kinds(p)
    assert not (config.SNAPSHOT_DIR / config.UNKNOWN_SNAPSHOT_FOLDER).exists()


def test_switching_on_and_off_needs_no_restart(make_pipeline, monkeypatch):
    monkeypatch.setattr(config, "UNKNOWN_SWITCH_SECONDS", 0)
    p = make_pipeline(on=False)
    frames(p, STRANGER, 4)
    assert visits() == []
    conn = db.connect()
    db.set_unknown_log_enabled(conn, True, "visitors expected", "admin")
    frames(p, STRANGER, 1, start=0.5)  # the same track, still in view: recorded now
    assert len(visits()) == 1
    db.set_unknown_log_enabled(conn, False, "not needed", "admin")
    conn.close()
    leave(p, start=1)
    frames(p, OTHER_STRANGER, 5, start=5)  # another stranger: the log is off again
    assert len(visits()) == 1


def test_known_and_uncertain_faces_are_never_visitors(make_pipeline):
    p = make_pipeline()
    frames(p, unit(0), 6)  # waqas: an event, not a visit
    assert "event" in kinds(p)
    leave(p, start=1)
    frames(p, UNCERTAIN, 10, start=5)  # 0.40: "unknown", but too close to waqas to be called a stranger
    assert visits() == [] and "visitor" not in kinds(p)


def test_a_stranger_who_waits_or_comes_back_is_one_visit(make_pipeline, monkeypatch):
    monkeypatch.setattr(config, "UNKNOWN_MERGE_SECONDS", 60)
    p = make_pipeline()
    frames(p, STRANGER, 4)
    leave(p, start=0.5)
    frames(p, STRANGER, 4, start=10)  # back 10 s later: the same face
    [v] = visits()
    assert v["tracks"] == 2 and v["first_seen"] == "2026-10-20 09:00:00"
    assert [d["detail"] for d in p.live.decisions if d["kind"] == "visitor"][-1] == "same unknown visitor (pass 2)"
    leave(p, start=10.5)
    assert visits()[0]["last_seen"] == "2026-10-20 09:00:10"
    frames(p, OTHER_STRANGER, 4, start=15)  # someone else: a new visit
    leave(p, start=15.5)
    frames(p, STRANGER, 4, start=100)  # the first one again, after the window: a new visit too
    assert [v["tracks"] for v in visits()] == [2, 1, 1]


def test_a_track_recognised_after_all_withdraws_its_visit(make_pipeline, monkeypatch, capsys):
    monkeypatch.setattr(config, "UNKNOWN_MIN_FRAMES", 1)
    p = make_pipeline()
    frames(p, STRANGER, 1)  # one dark frame: recorded
    [v] = visits()
    snap = config.DATA_DIR / v["snapshot_path"]
    assert snap.exists()
    frames(p, unit(0), 3, start=0.125)  # then three clear frames of waqas: his vote is won
    assert visits() == [] and not snap.exists()
    assert "recognised as waqas after all: unknown visitor withdrawn" in capsys.readouterr().out
    conn = db.connect()
    assert [e["employee_code"] for e in db.recent_events(conn)] == ["waqas"]  # his event is recorded as usual
    conn.close()


def test_a_reviewed_visit_is_never_withdrawn(make_pipeline, monkeypatch):
    monkeypatch.setattr(config, "UNKNOWN_MIN_FRAMES", 1)
    p = make_pipeline()
    frames(p, STRANGER, 1)
    [v] = visits()
    conn = db.connect()
    db.review_unknown_visit(conn, v["id"], "dismissed", None, None, "a delivery", "guard")
    conn.close()
    frames(p, unit(0), 3, start=0.125)
    assert [x["status"] for x in visits()] == ["dismissed"] and (config.DATA_DIR / v["snapshot_path"]).exists()


def test_spoofs_are_never_visitors_and_live_frames_are_needed(make_pipeline):
    p = make_pipeline(liveness=FakeLiveness(0.1))
    frames(p, STRANGER, 8)  # a photo of a stranger held up to the camera
    assert visits() == []
    p = make_pipeline(liveness=FakeLiveness(0.95))
    frames(p, STRANGER, 3, start=20)
    [v] = visits()
    assert v["liveness"] == 0.95


def test_a_visit_the_database_refuses_waits_and_is_written_once(make_pipeline, monkeypatch):
    p = make_pipeline()
    real, calls = db.add_unknown_visit, []

    def locked(*args):
        calls.append(args[-1])  # retry
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "add_unknown_visit", locked)
    frames(p, STRANGER, 5)
    assert visits() == [] and len(p.visitors.pending) == 1
    monkeypatch.setattr(db, "add_unknown_visit", lambda *args: (calls.append(args[-1]), real(*args))[1])
    p.visitors.housekeeping()
    [v] = visits()
    assert not p.visitors.pending and calls == [False, True]  # retried, looking for the visit first
    assert (config.DATA_DIR / v["snapshot_path"]).exists()


def test_a_retried_visit_is_never_written_twice():
    conn = db.connect()
    cam = db.upsert_camera(conn, "gate", 0, "IN")
    first = db.add_unknown_visit(conn, cam, T0, T0, 1, 0.2, "waqas", None, 200, "snapshots/unknown/x.jpg")
    again = db.add_unknown_visit(conn, cam, T0, T0 + timedelta(seconds=9), 2, 0.2, "waqas", None, 200,
                                 "snapshots/unknown/x.jpg", retry=True)
    rows = conn.execute("SELECT id, last_seen, tracks FROM unknown_visits").fetchall()
    conn.close()
    assert again == first and [tuple(r) for r in rows] == [(first, "2026-10-20 09:00:09", 2)]


def test_a_visit_not_written_before_the_log_is_switched_off_is_dropped(make_pipeline, monkeypatch):
    p = make_pipeline()
    real = db.add_unknown_visit

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "add_unknown_visit", locked)
    frames(p, STRANGER, 5)
    snapshot = p.visitors.pending[0].snapshot
    assert (config.DATA_DIR / snapshot).exists()
    conn = db.connect()
    db.set_unknown_log_enabled(conn, False, "privacy", "admin")
    conn.close()
    monkeypatch.setattr(db, "add_unknown_visit", real)
    monkeypatch.setattr(config, "UNKNOWN_SWITCH_SECONDS", 0)
    p.visitors.housekeeping()
    assert visits() == [] and not p.visitors.pending and not (config.DATA_DIR / snapshot).exists()


def test_the_stop_writes_open_visits_last_seen(make_pipeline):
    p = make_pipeline()
    frames(p, STRANGER, 10)  # still in view at 1.125 s when the worker stops
    p.visitors.finish()
    [v] = visits()
    assert (v["first_seen"], v["last_seen"]) == ("2026-10-20 09:00:00", "2026-10-20 09:00:01")


def test_worker_run_records_a_stranger_from_a_clip(tmp_path, monkeypatch, capsys):
    """recognize.run end to end: the visitor log read at the start, a stranger in a 1080p clip recorded with a
    full-resolution snapshot, and no event."""
    clip = tmp_path / "gate.avi"
    w = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"MJPG"), 25, (1920, 1080))
    for i in range(15):  # 5 processed frames
        w.write(full_hd(seed=i))
    w.release()
    Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]}).save()
    conn = db.connect()
    db.sync_persons(conn, ["waqas", "ali"])
    db.set_unknown_log_enabled(conn, True, "test", "admin")
    conn.close()
    monkeypatch.setattr(recognize, "Detector", lambda: SmallFaceDetector())
    monkeypatch.setattr(recognize, "Recognizer", lambda: RecordingRecognizer(STRANGER))
    monkeypatch.setattr(recognize, "providers_in_use", lambda: {"detection": ["CPUExecutionProvider"]})
    monkeypatch.setattr(recognize, "Liveness", lambda: FakeLiveness(0.97))
    cam = {"name": "gate", "source": str(clip), "direction": "IN"}
    runtime, live = workers.WorkerRuntime("gate", str(clip)), LiveWriter("gate")
    try:
        assert recognize.run(argparse.Namespace(no_db=False, no_window=True), cam, str(clip), runtime,
                             live) == "end of video"
    finally:
        runtime.close("end of video")
    out = capsys.readouterr().out
    assert "Unknown visitor log ON" in out and "UNKNOWN VISITOR #1" in out
    [v] = visits()
    assert (v["face_px"], v["liveness"], v["closest_code"]) == (150, 0.97, "waqas")
    snap = cv2.imread(str(config.DATA_DIR / v["snapshot_path"]))
    assert max(snap.shape[:2]) == config.UNKNOWN_SNAPSHOT_SIZE  # from the 1920 px frame
    conn = db.connect()
    assert db.recent_events(conn) == []
    conn.close()


def test_no_db_has_no_visitor_log(tmp_path, monkeypatch, capsys):
    Gallery.from_people({"waqas": [unit(0)]}).save()
    monkeypatch.setattr(recognize, "Detector", FakeDetector)
    monkeypatch.setattr(recognize, "Recognizer", lambda: FixedRecognizer(STRANGER))
    monkeypatch.setattr(recognize, "providers_in_use", lambda: {})
    monkeypatch.setattr(config, "LIVENESS_ENABLED", False)
    cam = {"name": "gate", "source": str(tmp_path / "missing.avi"), "direction": "IN"}
    runtime = workers.WorkerRuntime("gate", cam["source"])
    try:
        recognize.run(argparse.Namespace(no_db=True, no_window=True), cam, cam["source"], runtime, LiveWriter("gate"))
    finally:
        runtime.close("test")
    assert "Unknown visitor log" not in capsys.readouterr().out


# --- The API ------------------------------------------------------------------------------------------------------

TODAY = date.today()


def snapshot_file(day: date, name: str) -> str:
    path = config.SNAPSHOT_DIR / config.UNKNOWN_SNAPSHOT_FOLDER / day.isoformat() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", np.full((40, 40, 3), 90, np.uint8))
    path.write_bytes(buf.tobytes())
    return path.relative_to(config.DATA_DIR).as_posix()


@pytest.fixture
def app():
    application = create_app()
    with TestClient(application):
        conn = db.connect()
        db.sync_persons(conn, ["1001", "1002"])
        conn.execute("UPDATE persons SET name = 'Ayesha' WHERE employee_code = '1001'")
        conn.execute("UPDATE persons SET name = 'Bilal' WHERE employee_code = '1002'")
        conn.commit()
        gate, back = db.upsert_camera(conn, "gate", 0, "IN"), db.upsert_camera(conn, "back", 1, "OUT")
        now = datetime.combine(TODAY, datetime.min.time()) + timedelta(hours=9)
        yesterday = now - timedelta(days=1)
        application.state.visits = {
            "a": db.add_unknown_visit(conn, gate, now, now + timedelta(minutes=2), 1, 0.31, "1001", 0.93, 160,
                                      snapshot_file(TODAY, "a.jpg")),
            "b": db.add_unknown_visit(conn, back, now + timedelta(hours=1), now + timedelta(hours=1), 3, 0.12, None,
                                      None, 120, None),
            "old": db.add_unknown_visit(conn, gate, yesterday, yesterday, 1, 0.2, "1002", None, 140,
                                        snapshot_file(TODAY - timedelta(days=1), "old.jpg")),
        }
        application.state.people = db.person_ids(conn)
        conn.close()
        yield application


def audit(app, action: str) -> list[dict]:
    """The audit rows of `action`, newest first, details parsed."""
    return [{**e, "details": json.loads(e["details"])} for e in app.state.db.audit_entries() if e["action"] == action]


def test_admins_and_guards_list_visits_with_filters(app):
    guard = as_role(app, "guard")
    got = items(guard.get("/api/unknowns"))  # default: today, newest first
    assert [v["camera"] for v in got] == ["back", "gate"]
    a = next(v for v in got if v["camera"] == "gate")
    assert (a["closest_code"], a["closest_name"], a["score"], a["tracks"], a["liveness"], a["status"]) == (
        "1001", "Ayesha", 0.31, 1, 0.93, "new")
    assert a["snapshot_url"] == f"/api/unknowns/{a['id']}/snapshot" and a["direction"] == "IN"
    assert a["last_seen"].endswith("09:02:00")
    week = {"from": (TODAY - timedelta(days=6)).isoformat(), "to": TODAY.isoformat()}
    assert len(items(guard.get("/api/unknowns", params=week))) == 3
    assert [v["closest_name"] for v in items(guard.get("/api/unknowns", params={**week, "q": "bil"}))] == ["Bilal"]
    assert [v["tracks"] for v in items(guard.get("/api/unknowns", params={**week, "sort": "score"}))] == [3, 1, 1]
    gate = app.state.visits["a"]
    camera_id = items(guard.get("/api/unknowns"))[1]["camera_id"]
    assert [v["id"] for v in items(guard.get("/api/unknowns", params={"camera": camera_id}))] == [gate]
    assert items(guard.get("/api/unknowns", params={"status": "dismissed"})) == []
    assert guard.get("/api/unknowns", params={"sort": "nonsense"}).status_code == 422
    assert as_role(app, "admin").get(f"/api/unknowns/{gate}").json()["id"] == gate
    assert guard.get("/api/unknowns/99999").status_code == 404


def test_counts_for_the_menu(app):
    admin = as_role(app, "admin")
    assert admin.get("/api/unknowns/counts").json() == {"enabled": False, "new": 3, "today": 2}
    assert admin.put("/api/settings/unknowns", json={"enabled": True, "reason": "visitors expected"}).status_code == 200
    assert admin.get("/api/unknowns/counts").json()["enabled"] is True


def test_hr_and_viewers_never_see_visitors(app):
    for role in ("hr", "viewer"):
        c = as_role(app, role)
        for path in ("/api/unknowns", "/api/unknowns/counts", f"/api/unknowns/{app.state.visits['a']}",
                     f"/api/unknowns/{app.state.visits['a']}/snapshot", "/api/settings/unknowns"):
            assert c.get(path).status_code == 403, (role, path)
        assert c.patch(f"/api/unknowns/{app.state.visits['a']}",
                       json={"status": "dismissed", "reason": "x"}).status_code == 403


def test_snapshots_only_from_the_visitor_folder(app):
    guard = as_role(app, "guard")
    r = guard.get(f"/api/unknowns/{app.state.visits['a']}/snapshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"].startswith("private")
    assert guard.get(f"/api/unknowns/{app.state.visits['b']}/snapshot").status_code == 404  # none
    conn = db.connect()
    event_snapshot = config.SNAPSHOT_DIR / TODAY.isoformat() / "event.jpg"
    event_snapshot.parent.mkdir(parents=True, exist_ok=True)
    event_snapshot.write_bytes(b"jpeg")
    for path in (event_snapshot.relative_to(config.DATA_DIR).as_posix(), "snapshots/unknown/../../attendance.db",
                 "../outside.jpg"):
        conn.execute("UPDATE unknown_visits SET snapshot_path = ? WHERE id = ?", (path, app.state.visits["b"]))
        conn.commit()
        assert guard.get(f"/api/unknowns/{app.state.visits['b']}/snapshot").status_code == 404, path
    conn.close()


def test_reviews_are_audited_and_checked(app):
    guard, admin = as_role(app, "guard"), as_role(app, "admin")
    vid = app.state.visits["a"]
    url = f"/api/unknowns/{vid}"
    r = guard.patch(url, json={"status": "dismissed", "note": "courier", "reason": "checked at the gate"})
    assert r.status_code == 200 and (r.json()["status"], r.json()["note"], r.json()["reviewed_by"]) == (
        "dismissed", "courier", "guard")
    [entry] = audit(app, "review_unknown")
    assert (entry["user"], entry["target"], entry["reason"]) == ("guard", f"unknown:{vid}", "checked at the gate")
    assert entry["details"]["status"] == ["new", "dismissed"] and entry["details"]["note"] == [None, "courier"]
    assert entry["details"]["camera"] == "gate"

    assert guard.patch(url, json={"status": "dismissed", "reason": "again"}).status_code == 200  # note kept
    assert len(audit(app, "review_unknown")) == 1  # nothing changed: no audit row
    assert guard.patch(url, json={"status": "identified", "reason": "x"}).status_code == 422  # who?
    assert guard.patch(url, json={"status": "dismissed", "person_id": 1, "reason": "x"}).status_code == 422
    assert guard.patch(url, json={"status": "identified", "person_id": 999, "reason": "x"}).status_code == 404
    assert guard.patch(url, json={"status": "dismissed"}).status_code == 422  # no reason
    assert guard.patch(url, json={"status": "dismissed", "reason": "  "}).status_code == 422
    assert guard.patch("/api/unknowns/99999", json={"status": "dismissed", "reason": "x"}).status_code == 404

    bilal = app.state.people["1002"]
    r = admin.patch(url, json={"status": "identified", "person_id": bilal, "note": "", "reason": "it was Bilal"})
    assert (r.json()["status"], r.json()["person_code"], r.json()["person_name"], r.json()["note"]) == (
        "identified", "1002", "Bilal", None)
    assert audit(app, "review_unknown")[0]["details"]["person"] == [None, "1002"]
    r = admin.patch(url, json={"status": "new", "reason": "look again"})
    assert (r.json()["status"], r.json()["person_id"]) == ("new", None)
    conn = db.connect()
    assert conn.execute("SELECT count(*) FROM attendance_events").fetchone()[0] == 0  # never attendance
    conn.close()


def test_the_switch_is_admin_only_and_audited(app):
    guard, admin = as_role(app, "guard"), as_role(app, "admin")
    assert guard.get("/api/settings/unknowns").json()["enabled"] is False  # off by default
    assert guard.put("/api/settings/unknowns", json={"enabled": True, "reason": "x"}).status_code == 403
    assert admin.put("/api/settings/unknowns", json={"enabled": True}).status_code == 422  # no reason
    r = admin.put("/api/settings/unknowns", json={"enabled": True, "reason": "notice put up at the gate"})
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["updated_by"] == "admin"
    assert admin.put("/api/settings/unknowns", json={"enabled": True, "reason": "again"}).status_code == 200
    [entry] = audit(app, "enable_unknown_log")
    assert entry["details"] == {"unknown_visitors": ["off", "on"]} and entry["target"] == "settings:unknown_visitors"
    admin.put("/api/settings/unknowns", json={"enabled": False, "reason": "done"})
    assert len(audit(app, "disable_unknown_log")) == 1 and guard.get("/api/settings/unknowns").json()["enabled"] is False
    assert admin.get("/api/settings/shifts").json()["enabled"] is True  # the other switch is untouched


def test_unknown_settings_stay_below_the_match_and_the_kept_frames(app):
    admin = as_role(app, "admin")
    s = {x["key"]: x for x in admin.get("/api/settings").json()["settings"]}
    assert (s["unknown_min_frames"]["value"], s["unknown_max_score"]["value"], s["unknown_retention_days"]["value"]) \
        == (3, 0.35, 30)
    for values, message in (({"unknown_max_score": 0.5}, "unknown_max_score can't be more than match_threshold"),
                            ({"match_threshold": 0.3}, "unknown_max_score can't be more than match_threshold"),
                            ({"unknown_min_frames": 6}, "unknown_min_frames can't be more than best_frames"),
                            ({"unknown_retention_days": 91}, "between 1 and 90")):
        r = admin.patch("/api/settings", json={"values": values, "reason": "x"})
        assert r.status_code == 422 and message in r.json()["detail"], values
    r = admin.patch("/api/settings", json={"values": {"match_threshold": 0.3, "unknown_max_score": 0.25},
                                           "reason": "a darker gate"})
    assert r.status_code == 200 and config.UNKNOWN_MAX_SCORE == 0.25


def test_a_bad_settings_file_keeps_the_unknown_limits(capsys):
    config.SETTINGS_FILE.write_text(json.dumps({"settings": {"UNKNOWN_MAX_SCORE": 0.6, "MATCH_THRESHOLD": 0.5,
                                                             "UNKNOWN_MIN_FRAMES": 7, "BEST_FRAMES_PER_TRACK": 4}}))
    config.load_settings()
    assert (config.UNKNOWN_MAX_SCORE, config.UNKNOWN_MIN_FRAMES) == (0.35, 3)
    config.SETTINGS_FILE.write_text(json.dumps({"settings": {"MATCH_THRESHOLD": 0.3, "BEST_FRAMES_PER_TRACK": 2,
                                                             "VOTE_MIN_AGREE": 2}}))
    config.load_settings()
    assert (config.UNKNOWN_MAX_SCORE, config.UNKNOWN_MIN_FRAMES) == (0.3, 2)  # pulled under the limits
    assert "UNKNOWN_MAX_SCORE is more than MATCH_THRESHOLD" in capsys.readouterr().out


def test_retention_deletes_old_visits_and_their_snapshots(app):
    conn = db.connect()
    paths = {r["id"]: r["snapshot_path"] for r in conn.execute("SELECT id, snapshot_path FROM unknown_visits")}
    conn.close()
    old_snapshot, today_snapshot = (config.DATA_DIR / paths[app.state.visits[k]] for k in ("old", "a"))
    leftover = config.SNAPSHOT_DIR / config.UNKNOWN_SNAPSHOT_FOLDER / (TODAY - timedelta(days=40)).isoformat()
    leftover.mkdir(parents=True)
    (leftover / "never-written.jpg").write_bytes(b"x")
    event_day = config.SNAPSHOT_DIR / (TODAY - timedelta(days=40)).isoformat()
    event_day.mkdir(parents=True)

    kept = maintenance.clean_unknown_visits(app.state.db, TODAY, days=30)
    assert kept == {"visits": 0, "files": 1, "days": [leftover.name], "retention_days": 30}
    assert not leftover.exists() and old_snapshot.exists() and event_day.exists()  # event days aren't its business
    gone = maintenance.clean_unknown_visits(app.state.db, TODAY, days=0)  # everything first seen before today
    assert (gone["visits"], gone["files"]) == (1, 1) and not old_snapshot.exists() and today_snapshot.exists()
    assert sorted(v["id"] for v in items(as_role(app, "admin").get("/api/unknowns"))) == sorted(
        [app.state.visits["a"], app.state.visits["b"]])
    snaps = maintenance.clean_snapshots(app.state.db, TODAY, days=30)  # the event cleanup leaves visitors alone
    assert snaps["days"] == [event_day.name] and today_snapshot.exists()


def test_nightly_run_audits_the_visitor_cleanup(app, monkeypatch):
    monkeypatch.setattr(config, "UNKNOWN_RETENTION_DAYS", 1)
    conn = db.connect()
    cam = db.upsert_camera(conn, "gate", 0, "IN")
    long_ago = datetime.combine(TODAY - timedelta(days=5), datetime.min.time())
    db.add_unknown_visit(conn, cam, long_ago, long_ago, 1, 0.1, None, None, 100, None)
    conn.close()
    result = maintenance.run_nightly(app.state.db)
    assert result["unknowns"]["visits"] == 1, result
    [entry] = audit(app, "unknown_cleanup")
    assert entry["user"] == "system" and entry["reason"] == "older than 1 days"


def test_new_visits_reach_admins_and_guards_only(worker_app, monkeypatch):
    monkeypatch.setattr(config, "WS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(config, "WS_CAMERA_SECONDS", 0.2)
    guard, hr = as_role(worker_app, "guard"), as_role(worker_app, "hr")
    conn = db.connect()
    db.sync_persons(conn, ["1001"])
    cam = db.upsert_camera(conn, "a", 0, "IN")
    hub = worker_app.state.events
    with guard.websocket_connect("/api/ws") as g, hr.websocket_connect("/api/ws") as h:
        assert g.receive_json()["type"] == h.receive_json()["type"] == "hello"
        wait_for(lambda: hub._last is not None, timeout=5)  # the feed starts from the newest ids
        vid = db.add_unknown_visit(conn, cam, datetime.now(), datetime.now(), 1, 0.2, "1001", 0.9, 150, None)
        message = next_of(g, "unknown")
        assert (message["id"], message["camera"], message["closest_code"], message["status"]) == (vid, "a", "1001",
                                                                                                   "new")
        db.review_unknown_visit(conn, vid, "dismissed", None, None, "courier", "guard")
        changed = next_of(g, "unknowns_changed")
        assert (changed["action"], changed["target"], changed["user"]) == ("review_unknown", f"unknown:{vid}", "guard")
        eid = db.add_event(conn, db.person_ids(conn)["1001"], cam, datetime.now(), 0.8, None)
        seen = []
        for _ in range(400):  # HR gets the event, but never the visitor or the review
            m = h.receive_json()
            seen.append(m["type"])
            if m["type"] == "event" and m["id"] == eid:
                break
        assert "event" in seen and "unknown" not in seen and "unknowns_changed" not in seen
    conn.close()


def next_of(ws, kind: str, limit: int = 400) -> dict:
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"no {kind} message")
