"""Headless dashboard tests with Streamlit's AppTest, against a temporary database."""

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
