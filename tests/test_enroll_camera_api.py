"""Enrollment from an IP camera (api/enroll_camera.py, /api/enrollment/sources and /sessions): the camera choice and
the remembered last one, the server's own connection on the main stream (holding the camera like the webcam), a
running worker's shared frames, the live preview, checks and capture on the full-resolution window, release and
lapse, and the audited save."""

import base64
import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import enroll_camera
from api.main import create_app
from api.workers import WorkerManager
from app import camera_settings, config, live
from app.workers import WorkerManager as AppWorkerManager
from tests.test_api import FAKE, wait_for
from tests.test_data_api import as_role, clip, first_password, items, seed  # noqa: F401  (fixture)
from tests.test_web_api import PosingDetector, frame
from tests.fakes import FakeRecognizer

PASSWORD = "p@ss #1"


def full(step: str, seed: int = 0) -> np.ndarray:
    """A 1280x960 camera frame whose centred enrollment window (640x480) is a posed test frame."""
    img = np.random.default_rng(seed + 100).integers(40, 216, (960, 1280, 3), dtype=np.uint8)
    img[240:720, 320:960] = frame(step, seed)
    return img


class FakeReader:
    def __init__(self):
        self.frame, self.seq, self.stopped = full("front"), 1, False

    def show(self, img) -> None:
        self.frame, self.seq = img, self.seq + 1

    def latest(self, after=0):
        return (self.seq, self.frame) if self.frame is not None and self.seq > after else None

    def health(self):
        return {"last_error": None}

    def stop(self):
        self.stopped = True


class Opener:
    def __init__(self):
        self.calls, self.readers = [], []

    def __call__(self, source, name):
        self.calls.append((source, name))
        self.readers.append(FakeReader())
        return self.readers[-1]


@pytest.fixture
def opener():
    return Opener()


@pytest.fixture
def app(tmp_path, monkeypatch, opener):
    monkeypatch.setenv("GATESENSE_CAM_GATE_PASS", PASSWORD)
    monkeypatch.setattr(enroll_camera, "STALE_SHARE_SECONDS", 0.5)
    video = clip(tmp_path / "c.avi")
    gate = camera_settings.build({"name": "gate", "direction": "IN", "type": "hikvision", "ip": "10.0.0.5",
                                  "port": 554, "user": "gatesense", "channel": 1, "stream": "sub"}, [])
    camera_settings.save([
        gate,
        {"name": "desk", "direction": "IN", "mirror": False, "type": "webcam", "index": 0, "source": 0},
        {"name": "c", "direction": "OUT", "mirror": False, "type": "file", "path": str(video), "source": str(video)},
    ])
    run, logs, live_dir = tmp_path / "run", tmp_path / "logs", tmp_path / "live"
    inner = AppWorkerManager(None, script=FAKE, run_dir=run, log_dir=logs, live_dir=live_dir,
                             extra_args=("--no-window", "--run-dir", str(run), "--frames", "--live-dir", str(live_dir)))
    application = create_app(workers=WorkerManager(inner), models=lambda: (PosingDetector(), FakeRecognizer()),
                             enroll_opener=opener)
    try:
        with TestClient(application):
            application.state.seed = seed()
            application.state.ids = {c["name"]: c["id"] for c in items(as_role(application, "admin").get("/api/cameras"))}
            yield application
    finally:
        for proc in inner._procs.values():
            proc.kill()


def decode(data_url: str) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(base64.b64decode(data_url.split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)


def audit(app, action: str) -> list[dict]:
    return [e for e in app.state.db.audit_entries() if e["action"] == action]


def test_sources_list_ip_cameras_and_remember_the_last_choice(app):
    hr, viewer = as_role(app, "hr"), as_role(app, "viewer")
    assert viewer.get("/api/enrollment/sources").status_code == 403
    r = hr.get("/api/enrollment/sources")
    body = r.json()
    assert [(s["key"], s["kind"], s["label"]) for s in body["sources"]] == [
        ("webcam", "webcam", "This PC's webcam"), (f"camera:{app.state.ids['gate']}", "camera", "gate")]
    assert body["last"] is None and body["sources"][1]["direction"] == "IN"
    assert "rtsp" not in r.text and "10.0.0.5" not in r.text and "p@ss" not in r.text  # names only

    s = hr.post("/api/enrollment/sessions", json={"camera_id": app.state.ids["gate"]}).json()
    assert hr.get("/api/enrollment/sources").json()["last"] == f"camera:{app.state.ids['gate']}"
    assert as_role(app, "admin").get("/api/enrollment/sources").json()["last"] is None  # per user
    hr.delete(f"/api/enrollment/sessions/{s['session_id']}")
    claim = hr.post("/api/enrollment/webcam").json()
    hr.post("/api/enrollment/webcam", json={"claim_id": claim["claim_id"]})  # a renewal changes nothing
    assert hr.get("/api/enrollment/sources").json()["last"] == "webcam"
    assert hr.post("/api/enrollment/sessions", json={"camera_id": app.state.ids["desk"]}).status_code == 422


def test_own_connection_on_the_main_stream_holds_the_camera(app, opener):
    hr, admin = as_role(app, "hr"), as_role(app, "admin")
    gate = app.state.ids["gate"]
    r = hr.post("/api/enrollment/sessions", json={"camera_id": gate})
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["camera"], s["feed"]) == ("gate", "camera") and PASSWORD not in r.text
    assert s["renew_seconds"] == config.ENROLL_CAMERA_RENEW_SECONDS
    (source, name), = opener.calls
    assert name == "gate" and source.endswith("/Streaming/Channels/101")  # the main stream, not the sub (102)
    assert "p%40ss%20%231@" in source  # the password, URL-encoded, only inside the server

    busy = admin.post(f"/api/cameras/{gate}/start")
    assert busy.status_code == 409 and "in use by the enrollment wizard (hr)" in busy.json()["detail"]
    other = admin.post("/api/enrollment/sessions", json={"camera_id": gate})
    assert other.status_code == 409 and "in use by the enrollment wizard (hr)" in other.json()["detail"]
    assert admin.post(f"/api/enrollment/sessions/{s['session_id']}/check").status_code == 404  # not theirs

    renewed = hr.post("/api/enrollment/sessions", json={"camera_id": gate, "session_id": s["session_id"]}).json()
    assert renewed["session_id"] == s["session_id"] and len(opener.calls) == 1
    again = hr.post("/api/enrollment/sessions", json={"camera_id": gate}).json()  # a reload: the newest wins
    assert again["session_id"] != s["session_id"] and opener.readers[0].stopped
    assert hr.post(f"/api/enrollment/sessions/{s['session_id']}/check").status_code == 404

    assert hr.delete(f"/api/enrollment/sessions/{again['session_id']}").status_code == 204
    assert opener.readers[1].stopped
    assert admin.post(f"/api/cameras/{gate}/start").status_code == 200  # released: attendance can start


def test_checks_and_capture_use_the_full_resolution_window(app, opener):
    hr = as_role(app, "hr")
    s = hr.post("/api/enrollment/sessions", json={"camera_id": app.state.ids["gate"]}).json()
    sid, reader = s["session_id"], opener.readers[0]
    check = hr.post(f"/api/enrollment/sessions/{sid}/check", params={"step": "front"}).json()
    assert check["ok"] and check["connected"] and (check["width"], check["height"]) == (640, 480)
    assert check["face_px"] == 144 and check["feed"] == "camera"
    gx1, gy1, gx2, gy2 = check["guide"]
    x1, y1, x2, y2 = check["box"]
    assert gx1 <= (x1 + x2) / 2 <= gx2 and gy1 <= (y1 + y2) / 2 <= gy2
    wrong = hr.post(f"/api/enrollment/sessions/{sid}/capture", params={"step": "left"})
    assert wrong.status_code == 409 and "left" in wrong.json()["detail"].lower()

    photos = []
    for i, step in enumerate(("front", "left", "right")):
        reader.show(full(step, seed=i))
        r = hr.post(f"/api/enrollment/sessions/{sid}/capture", params={"step": step})
        assert r.status_code == 200, r.text
        shot = r.json()
        assert (shot["width"], shot["height"]) == (640, 480) and decode(shot["image"]).shape == (480, 640, 3)
        photos.append({"step": step, "image": shot["image"]})
    pid = app.state.seed["people"]["1004"]
    review = hr.post("/api/enrollment/review", params={"person_id": pid}, json={"photos": photos}).json()
    assert review["ready"], review
    r = hr.post(f"/api/people/{pid}/enroll", json={"photos": photos, "reason": "new starter", "source": "gate"})
    assert r.status_code == 200, r.text
    details = json.loads(audit(app, "enroll_person")[0]["details"])
    assert details["source"] == "gate" and details["steps"] == ["front", "left", "right"]


def test_the_preview_is_mjpeg_of_the_window(app):
    hr = as_role(app, "hr")
    sid = hr.post("/api/enrollment/sessions", json={"camera_id": app.state.ids["gate"]}).json()["session_id"]
    r = hr.get(f"/api/enrollment/sessions/{sid}/stream", params={"max_frames": 1})
    assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
    jpeg = r.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (480, 640, 3)
    assert as_role(app, "viewer").get(f"/api/enrollment/sessions/{sid}/stream").status_code == 403


def test_a_running_worker_shares_its_frames(app, opener):
    hr, admin = as_role(app, "hr"), as_role(app, "admin")
    gate = app.state.ids["gate"]
    assert admin.post(f"/api/cameras/{gate}/start").status_code == 200
    wait_for(lambda: admin.get(f"/api/cameras/{gate}").json()["worker"]["state"] == "running")
    s = hr.post("/api/enrollment/sessions", json={"camera_id": gate}).json()
    assert s["feed"] == "worker" and opener.calls == []  # no second connection
    check = wait_for(lambda: (c := hr.post(f"/api/enrollment/sessions/{s['session_id']}/check").json())["connected"]
                     and c)
    assert (check["width"], check["height"]) == (32, 24)  # the fake worker's 64x48 frames, full resolution
    assert admin.get(f"/api/cameras/{gate}").json()["worker"]["state"] == "running"  # attendance goes on

    admin.post(f"/api/cameras/{gate}/stop")  # the worker stops: the session reads the camera itself
    wait_for(lambda: len(opener.calls) == 1)
    assert hr.post(f"/api/enrollment/sessions/{s['session_id']}/check").json()["feed"] == "camera"
    assert not live.enroll_path("gate", app.state.workers.live_dir).exists()


def test_closing_a_shared_session_stops_the_sharing(app):
    hr, admin = as_role(app, "hr"), as_role(app, "admin")
    gate = app.state.ids["gate"]
    admin.post(f"/api/cameras/{gate}/start")
    wait_for(lambda: admin.get(f"/api/cameras/{gate}").json()["worker"]["state"] == "running")
    sid = hr.post("/api/enrollment/sessions", json={"camera_id": gate}).json()["session_id"]
    live_dir = app.state.workers.live_dir
    wait_for(lambda: live.full_path("gate", live_dir).exists())
    hr.delete(f"/api/enrollment/sessions/{sid}")
    assert not live.enroll_path("gate", live_dir).exists()
    wait_for(lambda: not live.full_path("gate", live_dir).exists())


def test_an_unrenewed_session_lapses(app, opener, monkeypatch):
    hr = as_role(app, "hr")
    monkeypatch.setattr(config, "ENROLL_CAMERA_CLAIM_SECONDS", 0.5)
    sid = hr.post("/api/enrollment/sessions", json={"camera_id": app.state.ids["gate"]}).json()["session_id"]
    wait_for(lambda: opener.readers[0].stopped)  # closed by the housekeeping, the browser never came back
    assert hr.post(f"/api/enrollment/sessions/{sid}/check").status_code == 404
    assert app.state.workers.camera_holds("gate") == []
