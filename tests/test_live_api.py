"""Phase 4: MJPEG stream and snapshot (fake camera workers reading a fake frame source), the shared per-camera
channel, the worker's watched mode, and the /api/ws WebSocket messages."""

import time
from datetime import datetime, timedelta

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api import auth
from api.live import Channel
from app import config, db, live
from tests.test_api import wait_for
from tests.test_data_api import as_role, at, first_password, items, worker_app  # noqa: F401  (fixtures)

TODAY = datetime.now().date()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(config, "WS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(config, "WS_CAMERA_SECONDS", 0.2)


def parts(body: bytes) -> list[bytes]:
    """The JPEGs in an MJPEG body."""
    out = []
    for chunk in body.split(b"--frame\r\n")[1:]:
        head, _, rest = chunk.partition(b"\r\n\r\n")
        length = int(dict(line.split(": ", 1) for line in head.decode().split("\r\n"))["Content-Length"])
        out.append(rest[:length])
    return out


def running_camera(app, client, name="c") -> int:
    camera_id = {c["name"]: c["id"] for c in items(client.get("/api/cameras"))}[name]
    assert client.post(f"/api/cameras/{camera_id}/start").status_code == 200
    wait_for(lambda: client.get(f"/api/cameras/{camera_id}").json()["worker"]["state"] == "running")
    wait_for(lambda: live.frame_path(name, app.state.workers.live_dir).exists())
    return camera_id


# --- Stream and snapshot --------------------------------------------------------------------


def test_stream_headers_and_frames(worker_app):
    a = as_role(worker_app, "admin")
    cid = running_camera(worker_app, a)
    with a.stream("GET", f"/api/cameras/{cid}/stream", params={"max_frames": 3}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
        assert r.headers["cache-control"] == "no-store"
        body = r.read()
    jpegs = parts(body)
    assert len(jpegs) == 3 and len(set(jpegs)) == 3  # three different frames
    for jpeg in jpegs:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape == (48, 64, 3)  # the fake source's frames


def test_watching_speeds_up_the_worker_and_stops_with_the_last_viewer(worker_app):
    a = as_role(worker_app, "admin")
    cid = running_camera(worker_app, a)
    live_dir = worker_app.state.workers.live_dir
    assert not live.is_watched("c", live_dir)
    with a.stream("GET", f"/api/cameras/{cid}/stream", params={"max_frames": 2}) as r:
        r.read()
        assert live.is_watched("c", live_dir)
    channel = worker_app.state.frames.channel("c")
    wait_for(lambda: not channel.running, timeout=5)  # no viewer: no reading, no .watch file
    assert not live.watch_path("c", live_dir).exists()


def test_snapshot(worker_app):
    a = as_role(worker_app, "admin")
    cid = running_camera(worker_app, a)
    r = a.get(f"/api/cameras/{cid}/snapshot.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR).shape == (48, 64, 3)


def test_width_limit(worker_app, monkeypatch):
    monkeypatch.setattr(config, "STREAM_MAX_WIDTH", 32)
    a = as_role(worker_app, "admin")
    cid = running_camera(worker_app, a)
    img = cv2.imdecode(np.frombuffer(a.get(f"/api/cameras/{cid}/snapshot.jpg").content, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (24, 32, 3)


def test_stream_access(worker_app):
    a = as_role(worker_app, "admin")
    ids = {c["name"]: c["id"] for c in items(a.get("/api/cameras"))}
    assert a.get(f"/api/cameras/{ids['a']}/stream").status_code == 409  # not running
    assert a.get(f"/api/cameras/{ids['a']}/snapshot.jpg").status_code == 404
    assert TestClient(worker_app).get(f"/api/cameras/{ids['c']}/stream").status_code == 401
    assert as_role(worker_app, "viewer").get(f"/api/cameras/{ids['c']}/stream").status_code == 403  # no live:view
    assert as_role(worker_app, "guard").get(f"/api/cameras/{ids['c']}/snapshot.jpg").status_code == 404  # allowed
    assert a.get("/api/cameras/999/stream").status_code == 404


def test_one_read_per_frame_whatever_the_viewers(tmp_path):
    """Many viewers share one channel: each new frame is read (and encoded, if too wide) once."""
    ch = Channel("gate", tmp_path)
    for _ in range(5):
        ch.join()
    for i in range(3):
        ok, buf = cv2.imencode(".jpg", np.full((48, 64, 3), 40 * i, np.uint8))
        from app.fileutil import atomic_write_bytes
        atomic_write_bytes(live.frame_path("gate", tmp_path), buf.tobytes())
        wait_for(lambda i=i: (ch.latest() or (0,))[0] == i + 1, timeout=5)
        time.sleep(0.05)
    assert ch.decoded == 3 and ch.seq == 3
    assert live.is_watched("gate", tmp_path)
    for _ in range(5):
        ch.leave()
    wait_for(lambda: not ch.running, timeout=5)
    assert not live.watch_path("gate", tmp_path).exists()


def test_live_writer_writes_faster_while_watched(tmp_path):
    t = [0.0]
    w = live.LiveWriter("gate", live_dir=tmp_path, clock=lambda: t[0])
    img = np.zeros((8, 8, 3), np.uint8)
    assert w.frame(img)
    t[0] += 0.2
    assert not w.frame(img)  # LIVE_FRAME_SECONDS (0.5 s) not up yet
    live.mark_watched("gate", tmp_path)
    t[0] += 0.5
    assert w.frame(img)
    t[0] += 1.0 / config.STREAM_FPS
    assert w.frame(img)  # every 1 / STREAM_FPS while watched
    live.unwatch("gate", tmp_path)
    t[0] += 0.5
    w.frame(img)
    t[0] += 1.0 / config.STREAM_FPS
    assert not w.frame(img)  # back to LIVE_FRAME_SECONDS


def test_watched_view_is_drawn_from_the_full_frame(tmp_path):
    from tests.test_pipeline import RecordingRecognizer, SmallFaceDetector, StubRecorder, full_hd, recognize, unit
    from app.gallery import Gallery

    writer = live.LiveWriter("gate", live_dir=tmp_path)
    p = recognize.Pipeline(Gallery.from_people({"waqas": [unit(0)]}), SmallFaceDetector(), RecordingRecognizer(unit(0)),
                           StubRecorder(), writer)
    assert p.process(full_hd(), 0.0, datetime(2026, 10, 20, 9), "t=0").shape == (360, 640, 3)
    live.mark_watched("gate", tmp_path)
    writer._watch_checked = None
    view = p.process(full_hd(seed=1), 0.1, datetime(2026, 10, 20, 9), "t=0.1")
    assert view.shape == (540, 960, 3)  # STREAM_MAX_WIDTH, from the 1920 px frame
    green = (view[:, :, 1] > 150) & (view[:, :, 0] < 80) & (view[:, :, 2] < 80)
    assert green[150, 300:375].mean() > 0.5  # the face box (200..250 at 640 px) scaled to 300..375


# --- WebSocket --------------------------------------------------------------------------------


def next_of(ws, kind: str, limit: int = 200) -> dict:
    """The next message of this type (camera messages arrive every 0.2 s, so this never waits for long)."""
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"no {kind} message")


@pytest.fixture
def app(worker_app):  # the live hub runs inside the app's lifespan
    return worker_app


def test_websocket_messages(app):
    guard, hr = as_role(app, "guard"), as_role(app, "hr")
    conn = db.connect()
    db.sync_persons(conn, ["1001"])
    pid = db.person_ids(conn)["1001"]
    cam = db.upsert_camera(conn, "a", 0, "IN")
    with guard.websocket_connect("/api/ws") as ws:
        assert ws.receive_json() == {"type": "hello", "user": "guard", "role": "guard"}
        cams = {}
        while len(cams) < 2:
            m = next_of(ws, "camera")
            cams[m["name"]] = m
        assert cams["a"]["status"] == "stopped" and cams["a"]["id"] == cam and "fps" in cams["a"] and "error" in cams["a"]

        eid = db.add_event(conn, pid, cam, datetime.now(), 0.73, None)  # as a camera worker would
        event = next_of(ws, "event")
        assert (event["id"], event["employee_code"], event["direction"], event["score"]) == (eid, "1001", "IN", 0.73)

        r = hr.post("/api/attendance/corrections", json={"action": "change", "event_id": eid,
                                                         "timestamp": at(TODAY, "00:01").isoformat(),
                                                         "reason": "clock was wrong"})
        assert r.status_code == 201
        changed = next_of(ws, "attendance_changed")
        assert (changed["action"], changed["target"], changed["user"]) == ("edit_event", f"event:{eid}", "hr")

        db.set_event_voided(conn, eid, True, "test from the dashboard", "admin")  # Streamlit's Corrections page
        assert next_of(ws, "attendance_changed")["action"] == "void_event"
    conn.close()


def test_websocket_many_clients_and_disconnects(app):
    clients = [as_role(app, "viewer")] + [TestClient(app) for _ in range(4)]
    for c in clients[1:]:
        c.cookies = clients[0].cookies
    hub = app.state.events
    sockets = [c.websocket_connect("/api/ws") for c in clients]
    opened = [s.__enter__() for s in sockets]
    for ws in opened:
        assert ws.receive_json()["type"] == "hello"
    wait_for(lambda: len(hub.clients) == 5, timeout=5)
    for s in sockets[:3]:
        s.__exit__(None, None, None)
    wait_for(lambda: len(hub.clients) == 2, timeout=5)
    for ws in opened[3:]:
        assert next_of(ws, "camera")["type"] == "camera"  # the others carry on
    for s in sockets[3:]:
        s.__exit__(None, None, None)
    wait_for(lambda: not hub.clients, timeout=5)


def test_websocket_refused_without_a_session_or_from_another_site(app):
    with pytest.raises(WebSocketDisconnect) as e:
        with TestClient(app).websocket_connect("/api/ws"):
            pass
    assert e.value.code == 1008
    viewer = as_role(app, "viewer")
    with pytest.raises(WebSocketDisconnect):
        with viewer.websocket_connect("/api/ws", headers={"origin": "http://evil.example"}):
            pass
    with viewer.websocket_connect("/api/ws", headers={"origin": "http://localhost:5173"}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_websocket_closed_when_the_session_ends(app, monkeypatch):
    viewer = as_role(app, "viewer")
    with viewer.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        monkeypatch.setattr(auth, "now", lambda: datetime.now() + timedelta(hours=9))
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(200):
                ws.receive_json()
        assert e.value.code == 4401
