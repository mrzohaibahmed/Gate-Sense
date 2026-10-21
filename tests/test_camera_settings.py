"""Camera settings (app/camera_settings.py): validation, cameras.json, passwords in .env, audit, test connection."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from app import camera_settings as cs
from app import config, db, sources, workers
from app.env import admin_password_source

ROOT = Path(__file__).resolve().parent.parent
HIK = {"name": "gate1", "direction": "IN", "type": "hikvision", "ip": "192.168.1.101", "port": 554,
       "user": "gatesense", "channel": 1, "stream": "main"}


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_cam_vars(monkeypatch):
    for name in ("GATESENSE_CAM_GATE1_PASS", "GATESENSE_CAM_GATE2_PASS", "GATESENSE_ADMIN_PASSWORD"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)  # restored (i.e. removed) after the test even if code set it


def audit(conn):
    return [dict(r) for r in db.audit_entries(conn)]


# --- URLs and validation ------------------------------------------------------------------------


def test_password_var_and_hikvision_url():
    assert cs.password_var("gate1") == "GATESENSE_CAM_GATE1_PASS"
    assert cs.password_var("main-entrance") == "GATESENSE_CAM_MAIN_ENTRANCE_PASS"
    assert cs.hikvision_source("192.168.1.101", 554, "gatesense", 1, "main", "V") == \
        "rtsp://gatesense:${V}@192.168.1.101:554/Streaming/Channels/101"
    assert cs.hikvision_source("10.0.0.9", 8554, "gate@sense", 3, "sub", "V") == \
        "rtsp://gate%40sense:${V}@10.0.0.9:8554/Streaming/Channels/302"


def test_build_hikvision():
    e = cs.build(HIK, [])
    assert e["source"] == "rtsp://gatesense:${GATESENSE_CAM_GATE1_PASS}@192.168.1.101:554/Streaming/Channels/101"
    assert "password" not in json.dumps(e).lower().replace("_pass}", "")


@pytest.mark.parametrize("change, message", [
    ({"name": ""}, "camera name"),
    ({"name": "gate 1"}, "camera name"),
    ({"name": "con"}, "camera name"),
    ({"name": "_dashboard"}, "camera name"),
    ({"direction": "SIDEWAYS"}, "direction"),
    ({"ip": "not an ip!"}, "IP address"),
    ({"port": 70000}, "port"),
    ({"user": " "}, "user name"),
    ({"channel": 0}, "channel"),
    ({"stream": "hd"}, "stream"),
    ({"type": "fax"}, "unknown camera type"),
])
def test_build_rejects(change, message):
    with pytest.raises(ValueError, match=message):
        cs.build({**HIK, **change}, [])


def test_build_rejects_duplicate_name():
    with pytest.raises(ValueError, match="already a camera called"):
        cs.build({**HIK, "name": "Gate1"}, [{"name": "gate1"}])


@pytest.mark.parametrize("url, ok", [
    ("rtsp://user:${GATESENSE_CAM_PASS}@192.168.1.5:554/live", True),
    ("rtsp://192.168.1.5:554/live", True),
    ("rtsp://user@192.168.1.5/live", True),
    ("rtsp://user:secret@192.168.1.5:554/live", False),  # typed-in password
    ("rtsp://user:${A}x@host/live", False),
    ("http://192.168.1.5/video", False),
])
def test_rtsp_url_rules(url, ok):
    entry = {"name": "cam", "direction": "OUT", "type": "rtsp", "url": url}
    if ok:
        assert cs.build(entry, [])["source"] == url
    else:
        with pytest.raises(ValueError):
            cs.build(entry, [])


def test_webcam_and_file():
    assert cs.build({"name": "w", "direction": "IN", "type": "webcam", "index": 1}, [])["source"] == 1
    with pytest.raises(ValueError, match="0 to 9"):
        cs.build({"name": "w", "direction": "IN", "type": "webcam", "index": 12}, [])
    assert cs.build({"name": "f", "direction": "OUT", "type": "file", "path": '"D:\\clips\\x.mp4"'}, [])[
        "source"] == "D:\\clips\\x.mp4"


# --- cameras.json and config -------------------------------------------------------------------------


def test_defaults_until_first_save():
    assert not config.CAMERAS_FILE.exists()
    names = [(e["name"], e["type"], e["source"]) for e in cs.entries()]
    assert names == [("entrance", "webcam", 0), ("exit", "webcam", 0)]


def test_add_hikvision_with_password(conn):
    saved = cs.upsert(HIK, "admin", conn, password="p@ss #1'\\x")
    data = json.loads(config.CAMERAS_FILE.read_text())
    assert [c["name"] for c in data["cameras"]] == ["entrance", "exit", "gate1"]
    assert "p@ss" not in config.CAMERAS_FILE.read_text()  # never in cameras.json
    assert [c["name"] for c in config.CAMERAS] == ["entrance", "exit", "gate1"]  # reloaded in this process
    assert cs.password_status("GATESENSE_CAM_GATE1_PASS") == ".env"
    url = sources.expand(saved["source"])
    assert url == "rtsp://gatesense:p%40ss%20%231%27%5Cx@192.168.1.101:554/Streaming/Channels/101"
    [entry] = audit(conn)
    assert entry["action"] == "add_camera" and entry["target"] == "camera:gate1"
    assert "p@ss" not in entry["details"] and json.loads(entry["details"])["password_changed"] is True
    assert "GATESENSE_CAM_GATE1_PASS" not in os.environ  # .env is not loaded into the environment


def test_edit_keeps_password_and_logs_changes(conn):
    cs.upsert(HIK, "admin", conn, password="first")
    cs.upsert({**HIK, "ip": "192.168.1.102", "stream": "sub"}, "admin", conn, reason="moved to gate B")
    assert cs.password_status("GATESENSE_CAM_GATE1_PASS") == ".env"
    assert "first@192.168.1.102:554/Streaming/Channels/102" in sources.expand(config.CAMERAS[-1]["source"])
    entry = audit(conn)[0]
    details = json.loads(entry["details"])
    assert entry["action"] == "edit_camera" and entry["reason"] == "moved to gate B"
    assert details["changes"]["ip"] == ["192.168.1.101", "192.168.1.102"]
    assert details["password_changed"] is False and "first" not in entry["details"]


def test_running_camera_cannot_be_changed_or_deleted(conn):
    cs.upsert(HIK, "admin", conn, password="pw")
    with pytest.raises(ValueError, match="stop it on the Cameras page first"):
        cs.upsert({**HIK, "ip": "10.0.0.1"}, "admin", conn, running={"gate1"})
    with pytest.raises(ValueError, match="stop it"):
        cs.delete("gate1", "admin", conn, running={"gate1"})


def test_password_only_for_hikvision(conn):
    with pytest.raises(ValueError, match="only be entered for a Hikvision camera"):
        cs.upsert({"name": "w", "direction": "IN", "type": "webcam", "index": 1}, "admin", conn, password="pw")


def test_delete_and_move(conn):
    cs.upsert(HIK, "admin", conn, password="pw")
    cs.move("gate1", -1)
    assert [c["name"] for c in config.CAMERAS] == ["entrance", "gate1", "exit"]
    cs.delete("gate1", "admin", conn)
    assert [c["name"] for c in config.CAMERAS] == ["entrance", "exit"]
    assert audit(conn)[0]["action"] == "delete_camera"
    cs.delete("exit", "admin", conn)
    with pytest.raises(ValueError, match="at least one camera"):
        cs.delete("entrance", "admin", conn)


def test_example_file_is_valid_and_has_no_passwords():
    """cameras.example.json (committed) must load, pass the page's validation and hold no secrets."""
    path = ROOT / "cameras.example.json"
    cams = config.load_cameras(path)
    assert [c["name"] for c in cams] == ["entrance", "gate1", "gate1-exit"]
    raw = json.loads(path.read_text())["cameras"]
    for i, entry in enumerate(raw):
        assert cs.build(entry, raw[:i])["source"] == entry["source"]  # what the page would save
    assert not any(cs._literal_password(str(c["source"])) for c in raw)


def test_cameras_json_is_git_ignored():
    ignored = subprocess.run(["git", "check-ignore", "-q", "cameras.json"], cwd=ROOT).returncode == 0
    tracked_ok = subprocess.run(["git", "check-ignore", "-q", "cameras.example.json"], cwd=ROOT).returncode == 1
    assert ignored and tracked_ok


def test_hand_edits_and_deletion_are_picked_up(conn):
    cs.upsert({"name": "desk", "direction": "IN", "type": "webcam", "index": 1}, "admin", conn)
    assert not config.refresh_cameras()  # nothing changed since the save
    data = json.loads(config.CAMERAS_FILE.read_text())
    data["cameras"][-1]["direction"] = "OUT"
    config.CAMERAS_FILE.write_text(json.dumps(data))  # edited by hand while the dashboard runs
    assert config.refresh_cameras() and config.CAMERAS[-1]["direction"] == "OUT"
    config.CAMERAS_FILE.unlink()  # deleted: back to the defaults
    assert config.refresh_cameras() and [c["name"] for c in config.CAMERAS] == ["entrance", "exit"]
    assert [e["name"] for e in cs.entries()] == ["entrance", "exit"]


def test_broken_cameras_file_falls_back_to_defaults(capsys):
    config.CAMERAS_FILE.write_text("{not json")
    assert [c["name"] for c in config.load_cameras()] == ["entrance", "exit"]
    assert "could not read" in capsys.readouterr().out


# --- .env handling ------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["plain", "p@ss #1", "it's", 'say "hi"', "back\\slash\\", "ünïcode€", "=;$x"])
def test_env_values_round_trip_and_keep_other_lines(value):
    config.ENV_PATH.write_text("GATESENSE_ADMIN_PASSWORD=admin-pw\n# comment\nOTHER=1\n")
    cs.set_env_value("GATESENSE_CAM_GATE1_PASS", value)
    from dotenv import dotenv_values

    values = dotenv_values(config.ENV_PATH)
    assert values["GATESENSE_CAM_GATE1_PASS"] == value
    assert values["GATESENSE_ADMIN_PASSWORD"] == "admin-pw" and values["OTHER"] == "1"
    assert "# comment" in config.ENV_PATH.read_text()


def test_env_value_rejects_line_breaks_and_empty():
    for bad in ["two\nlines", ""]:
        with pytest.raises(ValueError):
            cs.set_env_value("GATESENSE_CAM_GATE1_PASS", bad)
    assert not config.ENV_PATH.exists()


def test_password_status_prefers_environment(monkeypatch):
    cs.set_env_value("GATESENSE_CAM_GATE1_PASS", "file")
    monkeypatch.setenv("GATESENSE_CAM_GATE1_PASS", "env")
    assert cs.password_status("GATESENSE_CAM_GATE1_PASS") == "environment"
    assert cs.password_status("GATESENSE_CAM_GATE2_PASS") is None


def test_dashboard_loads_only_the_admin_password_from_env_file():
    """Workers inherit the dashboard's environment; a camera password copied there would win over later edits."""
    config.ENV_PATH.write_text("GATESENSE_ADMIN_PASSWORD=admin-pw\nGATESENSE_CAM_GATE1_PASS=old\n")
    assert admin_password_source() == ".env"
    assert os.environ["GATESENSE_ADMIN_PASSWORD"] == "admin-pw"
    assert "GATESENSE_CAM_GATE1_PASS" not in os.environ
    cs.set_env_value("GATESENSE_CAM_GATE1_PASS", "new")
    assert "new@" in sources.expand("rtsp://u:${GATESENSE_CAM_GATE1_PASS}@h/x")


# --- Test connection --------------------------------------------------------------------------------------


class FakeFrames:
    def __init__(self, frame=None, error=None):
        self.frame, self.last_error, self.codec, self.stopped = frame, error, "h264", False

    def latest(self, after=0):
        return (1, self.frame) if self.frame is not None else None

    def stop(self):
        self.stopped = True


def test_connection_ok():
    frames = FakeFrames(np.zeros((1080, 1920, 3), np.uint8))
    r = cs.test_connection("rtsp://cam/x", frames=frames)
    assert r["ok"] and (r["width"], r["height"], r["codec"]) == (1920, 1080, "h264") and frames.stopped


def test_connection_failure_is_masked():
    frames = FakeFrames(error="open failed: cannot open rtsp://u:S3cretPw@10.0.0.5/x")
    r = cs.test_connection("rtsp://cam/x", timeout=0.2, frames=frames)
    assert not r["ok"] and "S3cretPw" not in r["error"] and "u:***@" in r["error"]


def test_connection_uses_unsaved_password_and_reports_missing():
    r = cs.test_connection("rtsp://u:${GATESENSE_CAM_GATE1_PASS}@h/x", timeout=0.2, frames=FakeFrames())
    assert "missing environment variable GATESENSE_CAM_GATE1_PASS" in r["error"]
    r = cs.test_connection("rtsp://u:${GATESENSE_CAM_GATE1_PASS}@h/x", timeout=0.2, frames=FakeFrames(),
                           extra={"GATESENSE_CAM_GATE1_PASS": "typed"})
    assert r["error"] == "no picture within 0.2 s"  # got past the variable check


def test_connection_missing_file():
    assert "video file not found" in cs.test_connection(r"D:\nope\x.mp4")["error"]


# --- The worker reads cameras.json --------------------------------------------------------------------------


def test_worker_uses_cameras_file(tmp_path, conn):
    cs.upsert({**HIK, "name": "gate2"}, "admin", conn)  # no password saved
    env = dict(os.environ, GATESENSE_DATA_DIR=str(tmp_path / "data"), GATESENSE_ENV_FILE=str(tmp_path / "no.env"),
               GATESENSE_CAMERAS_FILE=str(config.CAMERAS_FILE))
    exe = workers.python_command(sys.executable, env)
    proc = subprocess.run([exe, str(ROOT / "scripts" / "recognize.py"), "--camera", "gate2", "--no-window"],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    assert "missing environment variable GATESENSE_CAM_GATE2_PASS" in proc.stdout  # found gate2 in cameras.json
