"""Headless dashboard tests with Streamlit's AppTest, against a temporary database."""

import re
from datetime import datetime, time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app import config, db

MAIN = str(Path(__file__).resolve().parent.parent / "dashboard" / "main.py")
PASSWORD = "test-password"
PAGES = ["views/live.py", "views/today.py", "views/reports.py", "views/people.py", "views/enrollment.py",
         "views/cameras.py", "views/corrections.py", "views/settings.py"]


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "attendance.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    conn = db.connect()
    db.sync_persons(conn, ["waqas"])
    cam = db.upsert_camera(conn, "entrance", 0, "IN")
    db.add_event(conn, db.person_ids(conn)["waqas"], cam, datetime.now().replace(microsecond=0), 0.81, None)
    yield conn
    conn.close()


def logged_in(monkeypatch) -> AppTest:
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, PASSWORD)
    at = AppTest.from_file(MAIN, default_timeout=30).run()
    at.text_input(key="password").input(PASSWORD)
    at.button[0].click().run()
    assert not at.exception
    return at


def test_refuses_to_run_without_password(monkeypatch, tmp_db):
    monkeypatch.delenv(config.DASHBOARD_PASSWORD_ENV, raising=False)
    at = AppTest.from_file(MAIN, default_timeout=30).run()
    assert config.DASHBOARD_PASSWORD_ENV in at.error[0].value
    assert not at.text_input


def test_wrong_password_rejected(monkeypatch, tmp_db):
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, PASSWORD)
    at = AppTest.from_file(MAIN, default_timeout=30).run()
    at.text_input(key="password").input("nope")
    at.button[0].click().run()
    assert at.error[0].value == "Wrong password."
    assert "authenticated" not in at.session_state
    assert not at.dataframe  # no page content behind the login


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(monkeypatch, tmp_db, page):
    at = logged_in(monkeypatch)
    at.switch_page(page).run()
    assert not at.exception, at.exception
    assert at.title[0].value


def test_live_page_shows_event(monkeypatch, tmp_db):
    at = logged_in(monkeypatch)
    df = at.dataframe[0].value
    assert list(df["Name"]) == ["waqas"] and list(df["Direction"]) == ["IN"]


def test_enrollment_add_person(monkeypatch, tmp_db):
    at = logged_in(monkeypatch)
    at.switch_page("views/enrollment.py").run()
    code, name = at.text_input[0], at.text_input[1]  # form order: code, name, department, shift
    code.input("e042")
    name.input("Ayesha Khan")
    at.button(key="FormSubmitter:add_person-Add person").click().run()
    assert not at.exception
    assert at.success[0].value.startswith("Added Ayesha Khan (e042)")
    assert (config.KNOWN_DIR / "e042").is_dir()
    assert db.audit_entries(tmp_db)[0]["action"] == "add_person"
    assert at.session_state["enroll_person"] == "e042"
    assert any("usable photo" in w.value for w in at.warning)  # 0 photos: under-5 warning


def running_status(name: str = "entrance", started_ago: float = 30):
    from datetime import timedelta

    from app.workers import WorkerStatus

    return WorkerStatus(name, "running", 1234, datetime.now() - timedelta(seconds=started_ago), 7.9, 0, True)


def test_live_view_shows_frame_and_decisions(monkeypatch, tmp_db):
    import numpy as np

    from app import live
    from app.workers import WorkerManager

    monkeypatch.setattr(WorkerManager, "statuses", lambda self: [running_status()])
    w = live.LiveWriter("entrance")
    w.decision("skip", 1, detail="blurry (var 41 < 60)")
    w.decision("unknown", 1, "waqas", 0.41, None, "below threshold")
    w.decision("match", 2, "waqas", 0.62, None, "vote 1/3")
    w.frame(np.full((48, 64, 3), 120, np.uint8), fps=7.9)

    at = logged_in(monkeypatch)
    at.switch_page("views/live_view.py").run()
    assert not at.exception
    assert len(at.get("image")) == 1
    assert not [w for w in at.warning if "not sending" in w.value]
    df = at.dataframe[0].value
    assert list(df["Decision"]) == ["✅ match", "❌ unknown", "⏭ skip"]  # newest first
    assert list(df["Detail"]) == ["vote 1/3", "below threshold", "blurry (var 41 < 60)"]


def test_live_view_stale_frame(monkeypatch, tmp_db):
    import json

    import numpy as np

    from app import live
    from app.workers import WorkerManager

    monkeypatch.setattr(WorkerManager, "statuses", lambda self: [running_status()])
    live.LiveWriter("entrance").frame(np.full((48, 64, 3), 120, np.uint8))
    info = json.loads(live.info_path("entrance").read_text())
    info["frame_time"] -= 10
    live.info_path("entrance").write_text(json.dumps(info))

    at = logged_in(monkeypatch)
    at.switch_page("views/live_view.py").run()
    assert any(w.value == "camera not sending frames" for w in at.warning)


def test_live_view_without_running_cameras(monkeypatch, tmp_db):
    at = logged_in(monkeypatch)
    at.switch_page("views/live_view.py").run()
    assert "No camera is running" in at.info[0].value


def test_enrollment_camera_is_opt_in_and_blocks_webcam_workers(monkeypatch, tmp_db):
    from app.workers import WorkerManager

    started = []
    monkeypatch.setattr(WorkerManager, "start", lambda self, name: started.append(name))
    at = logged_in(monkeypatch)
    at.switch_page("views/enrollment.py").run()
    at.text_input[0].input("e042")
    at.text_input[1].input("Ayesha Khan")
    at.button(key="FormSubmitter:add_person-Add person").click().run()
    assert not at.get("camera_input")  # off by default: the page doesn't grab the webcam
    at.toggle(key="enroll_camera_on").set_value(True).run()
    assert at.get("camera_input")

    at.switch_page("views/cameras.py").run()  # navigating away releases the claim
    at.button(key="start_entrance").click().run()
    assert started == ["entrance"]


def test_webcam_worker_refused_while_enrollment_camera_on(monkeypatch, tmp_db):
    from app.workers import WorkerManager
    from dashboard import common

    started = []
    monkeypatch.setattr(config, "CAMERAS", [
        {"name": "entrance", "source": 0, "direction": "IN"},
        {"name": "exit", "source": __file__, "direction": "OUT"},  # an existing file stands in for a clip
    ])
    monkeypatch.setattr(WorkerManager, "start", lambda self, name: started.append(name))
    monkeypatch.setattr(common, "camera_claimed", lambda: True)
    monkeypatch.setattr("dashboard.views.cameras.camera_claimed", lambda: True, raising=False)
    at = logged_in(monkeypatch)
    common._camera_claims()["other-browser-tab"] = __import__("time").time()
    try:
        at.switch_page("views/cameras.py").run()
        at.button(key="start_entrance").click().run()
        assert started == []
        assert any("Enrollment page" in e.value for e in at.error)
        at.button(key="start_exit").click().run()  # a video file source isn't affected
        assert started == ["exit"]
    finally:
        common._camera_claims().clear()


def test_enrollment_camera_refused_while_webcam_worker_runs(monkeypatch, tmp_db):
    from app.workers import WorkerManager

    monkeypatch.setattr(WorkerManager, "webcam_users", lambda self: ["entrance"])
    at = logged_in(monkeypatch)
    at.switch_page("views/enrollment.py").run()
    assert any("in use by the entrance worker" in w.value for w in at.warning)
    assert not at.get("camera_input")


def test_cameras_page_lists_configured_cameras(monkeypatch, tmp_db):
    at = logged_in(monkeypatch)
    at.switch_page("views/cameras.py").run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    for cam in config.CAMERAS:
        assert cam["name"] in text
    assert [b.label for b in at.button if b.label in ("Start", "Stop")] == ["Start"] * len(config.CAMERAS)


def test_cameras_page_warns_about_missing_clip(monkeypatch, tmp_db):
    monkeypatch.setattr(config, "CAMERAS", [{"name": "exit", "source": "data/clips/none.mp4", "direction": "OUT"}])
    at = logged_in(monkeypatch)
    at.switch_page("views/cameras.py").run()
    assert any("Video file not found" in w.value and "none.mp4" in w.value for w in at.warning)
    at.button(key="start_exit").click().run()
    assert any("video file not found" in e.value for e in at.error)


RTSP = "rtsp://admin:S3cretPw@192.168.1.64:554/Streaming/Channels/101"


@pytest.mark.parametrize("health, badge, expected", [
    ({"kind": "rtsp", "state": "live", "width": 1920, "height": 1080, "input_fps": 25.0, "processed_fps": 8.0,
      "last_frame_at": None, "reconnects": 3, "reconnects_today": 2, "last_error": None, "live_since": None},
     "live", ["1920×1080", "input 25.0 fps / processed 8.0 fps", "reconnects today: 2"]),
    ({"kind": "rtsp", "state": "reconnecting", "width": None, "height": None, "input_fps": 0, "processed_fps": 0,
      "last_frame_at": None, "reconnects": 5, "reconnects_today": 5,
      "last_error": f"open failed: cannot open {RTSP}", "live_since": None},
     "reconnecting", ["reconnects today: 5", "last error: open failed: cannot open rtsp://admin:***@"]),
])
def test_cameras_page_feed_health(monkeypatch, tmp_db, health, badge, expected):
    import time

    from app.workers import WorkerManager, WorkerStatus

    health = dict(health, last_frame_at=time.time() - 0.4)
    monkeypatch.setattr(config, "CAMERAS", [{"name": "gate", "source": RTSP, "direction": "IN"}])
    monkeypatch.setattr(WorkerManager, "status", lambda self, name: WorkerStatus(
        "gate", "running", 99, datetime.now(), 8.0, RTSP, True, health=health))
    at = logged_in(monkeypatch)
    at.switch_page("views/cameras.py").run()
    text = " ".join(m.value for m in at.markdown)
    assert f"● {badge}]" in text
    for part in expected:
        assert part in text
    assert re.search(r"last frame \d+\.\d s ago", text)  # age depends on how long the page took to render
    assert "S3cretPw" not in text and "rtsp://admin:***@192.168.1.64" in text


def test_cameras_page_missing_camera_password(monkeypatch, tmp_db):
    from app.workers import WorkerManager

    monkeypatch.delenv("GATESENSE_CAM_PASS", raising=False)
    started = []
    monkeypatch.setattr(config, "CAMERAS", [{
        "name": "gate", "direction": "IN",
        "source": "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101"}])
    at = logged_in(monkeypatch)
    at.switch_page("views/cameras.py").run()
    assert any("missing environment variable GATESENSE_CAM_PASS" in e.value for e in at.error)
    assert "rtsp://gatesense:***@192.168.1.101" in " ".join(m.value for m in at.markdown)
    real_start = WorkerManager.start
    monkeypatch.setattr(WorkerManager, "start", lambda self, name: started.append(name) or real_start(self, name))
    at.button(key="start_gate").click().run()
    assert started == ["gate"] and not at.exception  # refused with a message, not a crash
    assert sum("missing environment variable" in e.value for e in at.error) >= 2


def test_cameras_page_offline_badge(monkeypatch, tmp_db):
    at = logged_in(monkeypatch)
    at.switch_page("views/cameras.py").run()
    assert "red-badge[● offline]" in " ".join(m.value for m in at.markdown)


def test_settings_page_masks_rtsp_password(monkeypatch, tmp_db):
    monkeypatch.setattr(config, "CAMERAS", [
        {"name": "gate", "source": "rtsp://admin:s3cret@10.0.0.5:554/stream1", "direction": "IN"}])
    at = logged_in(monkeypatch)
    at.switch_page("views/settings.py").run()
    sources = list(at.dataframe[-1].value["Source"])
    assert sources == ["rtsp://admin:***@10.0.0.5:554/stream1"]


def test_manual_event_needs_reason(monkeypatch, tmp_db):
    at = logged_in(monkeypatch)
    at.switch_page("views/corrections.py").run()
    at.time_input[0].set_value(time(17, 0))  # form values are only sent with the submit click
    at.button(key="FormSubmitter:add_event-Add event").click().run()
    assert "reason is required" in at.error[0].value.lower()
    assert db.audit_entries(tmp_db) == []

    at.time_input[0].set_value(time(17, 0))
    at.text_input[0].input("forgot the exit camera")
    at.button(key="FormSubmitter:add_event-Add event").click().run()
    assert not at.exception
    assert at.success[0].value.startswith("Added manual IN event")
    assert db.audit_entries(tmp_db)[0]["reason"] == "forgot the exit camera"
