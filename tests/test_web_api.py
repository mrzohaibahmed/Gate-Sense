"""Phase 6 API additions for the web dashboard: guided enrollment (check a frame, review, save), system info and
backup, the attendance department filter and the people photo thumbnail."""

import base64

import cv2
import numpy as np
import pytest

from api import auth
from app import config, enroll_guide, enrollment
from tests.fakes import FRONTAL_112, FakeLiveness, FakeRecognizer
from app.detector import Detection
from tests.test_data_api import as_role, first_password, items  # noqa: F401  (first_password: fixture)

MARK = {"front": 30, "left": 120, "right": 220}  # what the fake detector reads as the head turn
YAW = {"front": 0.0, "left": 25.0, "right": -25.0}  # the person's point of view: + = their own left


class PosingDetector:
    """One centred face; its head pose (yaw) is read from a grey block painted in the frame's corner."""

    def detect(self, img, det_thresh=None):
        h, w = img.shape[:2]
        size = int(h * 0.3)
        x, y = (w - size) / 2, (h - size) / 2
        return [Detection(np.array([x, y, x + size, y + size], np.float32), FRONTAL_112 * (size / 112) + [x, y], 0.9)]

    def pose(self, img, det):
        mark = float(img[5:15, 5:15].mean())
        step = min(MARK, key=lambda k: abs(MARK[k] - mark))
        return YAW[step], 0.0, "3D landmarks"  # raw image: image yaw = the person's yaw


def frame(step: str, seed: int = 0) -> np.ndarray:
    img = np.random.default_rng(seed).integers(40, 216, (480, 640, 3), dtype=np.uint8)
    img[0:20, 0:20] = MARK[step]
    return img


def jpeg(img) -> bytes:
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def b64(img) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg(img)).decode()


@pytest.fixture
def app():
    from fastapi.testclient import TestClient

    from api.main import create_app
    from tests.test_data_api import seed

    application = create_app(models=lambda: (PosingDetector(), FakeRecognizer()),
                             liveness=lambda: application.state.live)
    application.state.live = FakeLiveness()  # every face live; tests set their own scores
    with TestClient(application):
        application.state.seed = seed()
        yield application


def test_steps_and_frame_check(app):
    hr = as_role(app, "hr")
    assert [s["key"] for s in hr.get("/api/enrollment/steps").json()] == ["front", "left", "right"]
    check = lambda step, img: hr.post("/api/enrollment/check", params={"step": step}, content=jpeg(img),  # noqa: E731
                                      headers={"content-type": "image/jpeg"})
    r = check("front", frame("front"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["state"] == "good" and body["message"] == "Good, hold still"
    assert body["yaw"] == 0.0 and body["face_px"] == 144
    x1, y1, x2, y2 = body["box"]
    gx1, gy1, gx2, gy2 = body["guide"]
    assert gx1 <= (x1 + x2) / 2 <= gx2 and gy1 <= (y1 + y2) / 2 <= gy2
    wrong = check("left", frame("front")).json()  # facing front while the left step is asked for
    assert wrong["ok"] is False and wrong["state"] == "bad" and "left" in wrong["message"].lower()
    assert check("left", frame("left")).json()["ok"] is True
    assert check("sideways", frame("front")).status_code == 422
    assert hr.post("/api/enrollment/check", content=b"not an image",
                   headers={"content-type": "image/jpeg"}).status_code == 422
    assert as_role(app, "viewer").post("/api/enrollment/check", content=jpeg(frame("front"))).status_code == 403


def test_review_and_save(app):
    hr = as_role(app, "hr")
    pid = app.state.seed["people"]["1004"]
    photos = [{"step": s, "image": b64(frame(s, seed=i))} for i, s in enumerate(("front", "left"))]
    review = hr.post("/api/enrollment/review", params={"person_id": pid}, json={"photos": photos}).json()
    assert review["missing"] == ["right"] and review["ready"] is False
    assert [p["ok"] for p in review["photos"]] == [True, True] and review["photos"][1]["yaw"] == 25.0
    bad = [{"step": "right", "image": b64(frame("front", seed=5))}]  # facing front for the right step
    r = hr.post("/api/enrollment/review", json={"photos": bad}).json()
    assert r["photos"][0]["ok"] is False and r["photos"][0]["problem"]

    incomplete = hr.post(f"/api/people/{pid}/enroll", json={"photos": photos, "reason": "new starter"})
    assert incomplete.status_code == 422 and "no photo for right" in incomplete.json()["detail"]
    photos.append({"step": "right", "image": b64(frame("right", seed=9))})
    r = hr.post(f"/api/people/{pid}/enroll", json={"photos": photos})  # no reason needed: a default is audited
    assert r.status_code == 200, r.text
    assert r.json() == {"employee_code": "1004", "saved": ["1.jpg", "2.jpg", "3.jpg"], "accepted": 3, "embeddings": 3}
    stored = enrollment.read_sidecar(config.KNOWN_DIR / "1004")
    assert [stored[f]["step"] for f in ("1.jpg", "2.jpg", "3.jpg")] == ["front", "left", "right"]
    assert enrollment.load_gallery().counts() == {"1004": 3}
    entry = items(as_role(app, "admin").get("/api/audit", params={"action": "enroll_person"}))[0]
    assert entry["user"] == "hr" and entry["reason"] == "guided enrollment" and entry["details"]["steps"] == [
        "front", "left", "right"]
    person = next(p for p in items(hr.get("/api/people")) if p["employee_code"] == "1004")
    assert person["photo_url"] == f"/api/people/{pid}/photos/1.jpg" and person["embeddings"] == 3
    assert hr.get(person["photo_url"]).status_code == 200


def test_system_info_and_backup(app):
    a = as_role(app, "admin")
    info = a.get("/api/system").json()
    assert info["version"] == "0.1.0" and info["database_schema"] == 6 and info["events"] == 6 and info["people"] == 4
    assert info["database_bytes"] > 0 and info["backups"] == [] and info["cameras_running"] == 0
    r = a.post("/api/system/backup", json={"reason": "before the update"})
    assert r.status_code == 201
    backup = r.json()
    path = config.DB_PATH.parent / "backups" / backup["file"]
    assert path.is_file() and backup["size_bytes"] == path.stat().st_size
    import sqlite3

    with sqlite3.connect(path) as conn:  # a working copy with the events in it
        assert conn.execute("SELECT count(*) FROM attendance_events").fetchone()[0] == 6
    assert [b["file"] for b in a.get("/api/system").json()["backups"]] == [backup["file"]]
    entry = items(a.get("/api/audit", params={"action": "backup"}))[0]
    assert entry["reason"] == "before the update" and entry["details"]["file"] == backup["file"]
    assert as_role(app, "hr").get("/api/system").status_code == 403


def test_attendance_department_filter(app):
    v = as_role(app, "viewer")
    rows = items(v.get("/api/attendance", params={"department": "Stores", "sort": "name"}))
    assert [r["name"] for r in rows] == ["Ayesha", "Chand"]


def test_new_endpoints_need_a_login(app):
    from fastapi.testclient import TestClient

    anonymous = TestClient(app)
    for method, path in (("GET", "/api/enrollment/steps"), ("POST", "/api/enrollment/check"),
                         ("POST", "/api/enrollment/review"), ("POST", "/api/people/1/enroll"),
                         ("GET", "/api/system"), ("POST", "/api/system/backup")):
        assert anonymous.request(method, path).status_code == 401, path
    assert auth.PROTECTED_RESPONSES  # documented on every protected route
    assert enroll_guide.person_yaw(25.0) == 25.0  # the fake's convention: raw frames


# --- Liveness in the wizard ----------------------------------------------------------------------------------


def check(client, step, img):
    return client.post("/api/enrollment/check", params={"step": step}, content=jpeg(img),
                       headers={"content-type": "image/jpeg"})


def test_the_wizard_refuses_a_photo_or_a_screen(app):
    hr = as_role(app, "hr")
    assert check(hr, "front", frame("front")).json()["liveness"] == 0.99
    app.state.live = FakeLiveness(0.21)
    body = check(hr, "front", frame("front")).json()
    assert body["ok"] is False and body["message"] == "Use a real face, not a photo or a screen"
    assert body["liveness"] == 0.21
    pid = app.state.seed["people"]["1004"]
    photos = [{"step": s, "image": b64(frame(s, seed=i))} for i, s in enumerate(("front", "left", "right"))]
    review = hr.post("/api/enrollment/review", params={"person_id": pid}, json={"photos": photos}).json()
    assert not review["ready"] and all("not a real face" in p["problem"] for p in review["photos"])
    r = hr.post(f"/api/people/{pid}/enroll", json={"photos": photos})
    assert r.status_code == 422 and "not a real face" in r.json()["detail"]
    assert not enrollment.photo_paths(config.KNOWN_DIR / "1004")


def test_liveness_off_or_unavailable(app, monkeypatch):
    hr = as_role(app, "hr")
    monkeypatch.setattr(config, "LIVENESS_ENABLED", False)
    app.state.live = FakeLiveness(0.1)
    body = check(hr, "front", frame("front")).json()
    assert body["ok"] is True and body["liveness"] is None and app.state.live.calls == []
    monkeypatch.setattr(config, "LIVENESS_ENABLED", True)
    from app.liveness import LivenessUnavailable

    def missing():
        raise LivenessUnavailable("couldn't download MiniFASNetV2.onnx (offline)")

    monkeypatch.setattr(app.state, "liveness", missing)
    r = check(hr, "front", frame("front"))
    assert r.status_code == 503 and "liveness check can't run" in r.json()["detail"]


def test_uploaded_photo_files_are_not_liveness_checked(app):
    """Photo files (People page) may be a scan or a picture someone sent: only the wizard refuses spoofs."""
    hr = as_role(app, "hr")
    app.state.live = FakeLiveness(0.05)
    pid = app.state.seed["people"]["1004"]
    r = hr.post(f"/api/people/{pid}/photos", json={"photos": [{"name": "a.jpg", "image": b64(frame("front"))}]})
    assert r.status_code == 200 and r.json()["saved"] == 1 and app.state.live.calls == []
