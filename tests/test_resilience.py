"""Recording survives what goes wrong around it: durable file writes, a bad frame, failed live-view / status /
snapshot writes, a database that can't take an event right now, a damaged gallery, a broken cameras.json or a
damaged database at the start."""

import argparse
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import run, web
from api.main import create_app
from app import config, db, fileutil, live, workers
from app.gallery import Gallery, Match
from app.tracker import Sample, TrackState
from tests.fakes import FakeLiveness
from tests.test_pipeline import RecordingRecognizer, SmallFaceDetector, full_hd, unit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import recognize  # noqa: E402


# --- Durable writes --------------------------------------------------------------------------------------------


def test_atomic_writes_reach_the_disk_before_the_rename(tmp_path, monkeypatch):
    synced = []
    real = fileutil.os.fsync
    monkeypatch.setattr(fileutil.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    fileutil.atomic_write_bytes(tmp_path / "cameras.json", b"{}")
    assert (tmp_path / "cameras.json").read_bytes() == b"{}" and len(synced) == 1
    fileutil.atomic_write_bytes(tmp_path / "frame.jpg", b"jpeg", durable=False)  # live frames: no flush
    assert len(synced) == 1 and not list(tmp_path.glob("*.tmp"))


def test_gallery_save_reaches_the_disk(monkeypatch):
    import app.gallery as gallery_module

    synced = []
    real = gallery_module.os.fsync
    monkeypatch.setattr(gallery_module.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    Gallery.from_people({"waqas": [unit(0)]}).save()
    assert synced and Gallery.load().names == ["waqas"]


def test_error_log_prints_the_first_then_at_most_once_a_minute():
    clock, lines = [0.0], []
    log = fileutil.ErrorLog("writing x", clock=lambda: clock[0], log=lines.append)
    for _ in range(5):
        log(OSError("disk full"))
        clock[0] += 10
    assert len(lines) == 1 and "writing x failed: OSError: disk full; continuing" in lines[0]
    clock[0] += 60
    log(OSError("disk full"))
    assert len(lines) == 2 and "(5 since the last message)" in lines[1] and log.total == 6


# --- Live view and status writes never stop a worker --------------------------------------------------------------


def test_live_view_write_failures_are_logged_not_raised(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise PermissionError("held by a virus scanner")

    monkeypatch.setattr(live, "atomic_write_bytes", fail)
    writer = live.LiveWriter("gate", interval=0, background=False)
    writer.start()
    assert writer.frame(np.zeros((48, 64, 3), np.uint8), 8.0) is False
    writer.write_info(8.0, None)
    live.mark_enrolling("gate")
    assert writer.full_frame(np.zeros((48, 64, 3), np.uint8))  # handed over; the write fails quietly
    assert writer.write_errors.total == 4
    assert capsys.readouterr().out.count("held by a virus scanner") == 1


def test_status_write_failures_are_logged_not_raised(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(workers, "atomic_write_bytes", fail)
    runtime = workers.WorkerRuntime("gate", 0)
    runtime.write("running")
    runtime.close("stop requested")
    assert runtime.write_errors.total == 3


# --- The recorder: snapshots and the database -----------------------------------------------------------------


def track(name: str = "waqas") -> TrackState:
    t = TrackState(7)
    for i in range(3):
        t.add(Sample(0.9, Match(name, name, 0.8 + i / 100, None, None, "match"), np.full((20, 20, 3), 99, np.uint8)))
    return t


@pytest.fixture
def recorder():
    conn = db.connect()
    db.sync_persons(conn, ["waqas"])
    conn.close()
    rec = recognize.Recorder({"name": "gate", "direction": "IN"}, 0, use_db=True)
    yield rec
    rec.conn.close()


def events():
    conn = db.connect()
    try:
        return [dict(e) for e in db.recent_events(conn)]
    finally:
        conn.close()


def test_a_snapshot_that_cant_be_saved_never_costs_the_event(recorder, monkeypatch):
    def fail(path, img):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(recognize, "write_image", fail)
    kind, detail = recorder.record("09:00:00", datetime(2026, 10, 23, 9), track(), "waqas", 3)
    assert (kind, detail) == ("event", "IN saved (votes 3/3)")
    assert [(e["employee_code"], e["snapshot_path"]) for e in events()] == [("waqas", None)]
    assert recorder.snapshot_errors.total == 1


def test_events_wait_while_the_database_is_locked_then_are_written_in_order(recorder, monkeypatch, capsys):
    real, locked = db.add_camera_event, [True]

    def add_camera_event(*args):
        if locked[0]:
            raise sqlite3.OperationalError("database is locked")
        return real(*args)

    monkeypatch.setattr(db, "add_camera_event", add_camera_event)
    recorder.cooldown.seconds = 0  # the recorder took the cool-down when it was made
    monkeypatch.setattr(config, "COOLDOWN_SECONDS", 0)  # and the database check reads it at each event
    t0 = datetime(2026, 10, 23, 9)
    assert recorder.record("09:00:00", t0, track(), "waqas", 3) == ("event", "IN waiting for the database (votes 3/3)")
    recorder.record("09:00:05", t0 + timedelta(seconds=5), track(), "waqas", 3)
    assert len(recorder.pending) == 2 and events() == []
    recorder.flush()  # still locked: kept
    assert len(recorder.pending) == 2
    locked[0] = False
    recorder.flush()  # what the worker's housekeeping does every round
    assert not recorder.pending
    assert [e["timestamp"] for e in reversed(events())] == ["2026-10-23 09:00:00", "2026-10-23 09:00:05"]
    out = capsys.readouterr().out
    assert "EVENT waiting for the database" in out and "EVENT saved (waited since" in out
    assert out.count("database is locked") == 1  # the warning is not repeated on every retry


def test_the_waiting_queue_is_bounded(recorder, monkeypatch, capsys):
    monkeypatch.setattr(db, "add_camera_event",
                        lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    recorder.cooldown.seconds = 0  # the recorder took the cool-down when it was made
    monkeypatch.setattr(config, "COOLDOWN_SECONDS", 0)  # and the database check reads it at each event
    monkeypatch.setattr(config, "EVENT_QUEUE_MAX", 2)
    for i in range(3):
        recorder.record(f"09:00:0{i}", datetime(2026, 10, 23, 9, 0, i), track(), "waqas", 3)
    assert [e["time"].second for e in recorder.pending] == [1, 2]
    assert "EVENT LOST" in capsys.readouterr().out


def test_inactive_person_is_not_saved(recorder):
    conn = db.connect()
    pid = db.active_person_id(conn, "waqas")
    db.set_person_active(conn, pid, False, "left", "admin")
    conn.close()
    assert recorder.record("09:00:00", datetime(2026, 10, 23, 9), track(), "waqas", 3) == (
        "not saved", "person inactive or not enrolled")
    assert events() == [] and not recorder.pending


# --- The worker loop --------------------------------------------------------------------------------------------


class FlakyDetector(SmallFaceDetector):
    """Raises on the given calls (like an ONNX or OpenCV error on a strange frame), otherwise one face."""

    def __init__(self, fail_on: set[int]):
        super().__init__(50)
        self.fail_on, self.calls = fail_on, 0

    def detect(self, img, det_thresh=None):
        self.calls += 1
        if self.calls in self.fail_on:
            raise cv2.error("simulated decoder error")
        return super().detect(img, det_thresh)


def clip(tmp_path, frames: int) -> Path:
    path = tmp_path / "gate.avi"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 25, (1920, 1080))
    for i in range(frames):
        w.write(full_hd(seed=i))
    w.release()
    return path


def run_worker(tmp_path, monkeypatch, detector, frames: int = 24):
    source = clip(tmp_path, frames)
    Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]}).save()
    conn = db.connect()
    db.sync_persons(conn, ["waqas", "ali"])
    conn.close()
    monkeypatch.setattr(recognize, "Detector", lambda: detector)
    monkeypatch.setattr(recognize, "Recognizer", lambda: RecordingRecognizer(unit(0)))
    monkeypatch.setattr(recognize, "providers_in_use", lambda: {})
    monkeypatch.setattr(recognize, "Liveness", FakeLiveness)
    runtime, writer = workers.WorkerRuntime("gate", str(source)), live.LiveWriter("gate")
    try:
        reason = recognize.run(argparse.Namespace(no_db=False, no_window=True),
                               {"name": "gate", "source": str(source), "direction": "IN"}, str(source), runtime,
                               writer)
    finally:
        runtime.close("end of video")
    return reason, runtime, writer


def test_a_frame_that_fails_is_skipped_and_counted(tmp_path, monkeypatch, capsys):
    reason, runtime, writer = run_worker(tmp_path, monkeypatch, FlakyDetector({2, 3}))
    assert reason == "end of video"  # not "error": the worker kept going
    assert runtime.frame_errors == 2 and "simulated decoder error" in runtime.last_frame_error
    out = capsys.readouterr().out
    assert "Traceback" in out and out.count("processing a frame (frame skipped) failed") == 1
    assert [e["employee_code"] for e in events()] == ["waqas"]  # recorded from the frames that worked
    status = workers._read_json(workers.status_path("gate"))
    assert status["frame_errors"] == 2 and "simulated decoder error" in status["last_frame_error"]


def test_snapshot_and_live_view_failures_dont_stop_recording(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(recognize, "write_image", fail)
    monkeypatch.setattr(live, "atomic_write_bytes", fail)
    reason, runtime, writer = run_worker(tmp_path, monkeypatch, SmallFaceDetector(50))
    assert reason == "end of video" and runtime.frame_errors == 0 and writer.write_errors.total > 0
    assert [(e["employee_code"], e["snapshot_path"]) for e in events()] == [("waqas", None)]


def test_a_damaged_gallery_is_retried_until_it_can_be_read(monkeypatch, capsys):
    monkeypatch.setattr(config, "GALLERY_RETRY_SECONDS", 0.05)
    config.GALLERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GALLERY_PATH.write_bytes(b"PK\x03\x04 half a zip")  # what a power cut mid-copy can leave
    runtime = workers.WorkerRuntime("gate", 0)
    result = {}
    thread = threading.Thread(target=lambda: result.update(got=recognize.load_gallery(runtime)))
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive() and "gallery can't be read" in runtime.warning
    Gallery.from_people({"waqas": [unit(0)]}).save()  # restored, or enrollment saved it again
    thread.join(5)
    gallery, _ = result["got"]
    assert gallery.names == ["waqas"] and runtime.warning is None
    assert capsys.readouterr().out.count("reading the face gallery") == 1


def test_a_damaged_gallery_waits_for_a_stop_request():
    config.GALLERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GALLERY_PATH.write_bytes(b"not a zip")
    runtime = workers.WorkerRuntime("gate", 0)
    workers.stop_path("gate").touch()
    assert recognize.load_gallery(runtime) == (None, 0.0)


def test_reload_keeps_the_loaded_gallery_when_the_new_file_is_damaged():
    good = Gallery.from_people({"waqas": [unit(0)]})
    config.GALLERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GALLERY_PATH.write_bytes(b"PK\x03\x04 damaged")
    gallery, mtime = recognize.reload_gallery(good, 0.0)
    assert gallery is good and mtime == 0.0  # tried again at the next check


# --- Refusing to start ------------------------------------------------------------------------------------------


def test_quick_check(tmp_path):
    path = tmp_path / "a.db"
    assert db.quick_check(path) is None  # no database yet
    conn = db.connect(path)
    conn.close()
    assert db.quick_check(path) is None
    data = bytearray(path.read_bytes())
    data[:16] = b"this is not sql!"  # the header a power cut or a bad disk could leave
    path.write_bytes(bytes(data))
    assert db.quick_check(path)


@pytest.mark.sqlite_only  # a damaged SQLite file
def test_app_refuses_to_start_on_a_damaged_database_or_cameras_file(monkeypatch, capsys):
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, "first-admin-password")
    monkeypatch.setattr(run, "port_in_use", lambda: False)
    monkeypatch.setattr(web, "build_status", lambda: "ok")
    assert run.preflight(open_browser=False, build=False) == 0

    config.CAMERAS_FILE.write_text("{half")
    assert run.preflight(open_browser=False, build=False) == 1
    assert "cameras.json can't be read" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="cameras.json can't be read"):
        with TestClient(create_app()):
            pass
    config.CAMERAS_FILE.unlink()

    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.write_bytes(b"x" * 4096)
    assert run.preflight(open_browser=False, build=False) == 1
    out = capsys.readouterr().out
    assert "is damaged" in out and "restore.py --list" in out
    with pytest.raises(RuntimeError, match="damaged"):
        with TestClient(create_app()):
            pass


def test_worker_refuses_to_start_on_a_broken_cameras_file(monkeypatch, capsys):
    config.CAMERAS_FILE.write_text("")
    monkeypatch.setattr(sys, "argv", ["recognize.py", "--camera", "entrance", "--no-window"])
    assert recognize.main() == 1
    assert "can't be read" in capsys.readouterr().out
