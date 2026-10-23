import json
import threading
import time

import cv2
import numpy as np
import pytest

from app import config, fileutil, live
from app.camera import LiveCapture, is_live
from app.live import CameraHealth, LiveWriter


class Clock:
    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def frame(level: int = 120) -> np.ndarray:
    return np.full((48, 64, 3), level, np.uint8)


# --- Frame writer ------------------------------------------------------------------


def test_writer_is_throttled():
    clock = Clock()
    w = LiveWriter("cam", clock=clock, interval=0.5)
    assert w.frame(frame(), fps=8.0)
    clock.t += 0.3
    assert not w.frame(frame())  # too soon
    clock.t += 0.25
    assert w.frame(frame())  # 0.55 s after the first write


def test_writer_output_is_valid(tmp_path):
    w = LiveWriter("cam", live_dir=tmp_path, clock=Clock())
    w.decision("skip", 3, detail="blurry (var 41 < 60)")
    w.frame(frame(), fps=7.5)
    img = cv2.imdecode(np.fromfile(str(tmp_path / "cam.jpg"), np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (48, 64, 3)
    info = json.loads((tmp_path / "cam.json").read_text())
    assert info["fps"] == 7.5 and info["decisions"][0]["detail"] == "blurry (var 41 < 60)"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cam.jpg", "cam.json"]  # no .tmp files left


def test_writer_replaces_atomically(tmp_path, monkeypatch):
    """Each file is written to a temp name and moved into place with os.replace."""
    calls = []
    real = fileutil.os.replace

    def spy(src, dst):
        assert not str(src).endswith((".jpg", ".json"))  # never written in place
        calls.append((str(src), str(dst)))
        real(src, dst)

    monkeypatch.setattr(fileutil.os, "replace", spy)
    LiveWriter("cam", live_dir=tmp_path, clock=Clock()).frame(frame())
    assert [dst for _, dst in calls] == [str(tmp_path / "cam.jpg"), str(tmp_path / "cam.json")]
    assert all(src.endswith(".tmp") for src, _ in calls)


def test_reader_never_sees_partial_frame(tmp_path):
    """A reader polling while the writer rewrites the frame always gets a complete JPEG."""
    w = LiveWriter("cam", live_dir=tmp_path, interval=0)
    w.frame(np.random.default_rng(0).integers(0, 256, (480, 640, 3), dtype=np.uint8))
    bad, stop = [], threading.Event()

    def read():
        while not stop.is_set():
            try:
                data = (tmp_path / "cam.jpg").read_bytes()
            except OSError:
                continue
            if cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) is None:
                bad.append(len(data))

    reader = threading.Thread(target=read)
    reader.start()
    rng = np.random.default_rng(1)
    for _ in range(40):
        w.frame(rng.integers(0, 256, (480, 640, 3), dtype=np.uint8))
    stop.set()
    reader.join()
    assert bad == []


def test_replace_retries_while_target_is_locked(tmp_path, monkeypatch):
    attempts = []
    real = fileutil.os.replace

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(fileutil.os, "replace", flaky)
    fileutil.atomic_write_bytes(tmp_path / "x.bin", b"data")
    assert (tmp_path / "x.bin").read_bytes() == b"data" and len(attempts) == 3


def test_decisions_keep_last_20():
    w = LiveWriter("cam", clock=Clock())
    for i in range(25):
        w.decision("unknown", i, "waqas", 0.3, None, "below threshold")
    assert [d["track"] for d in w.decisions] == list(range(5, 25))


def test_clear_removes_files():
    w = LiveWriter("cam", clock=Clock())
    w.frame(frame())
    w.clear()
    assert not live.frame_path("cam").exists() and not live.info_path("cam").exists()


def test_info_update_keeps_frame_time():
    w = LiveWriter("cam", clock=Clock())
    w.frame(frame())
    first = json.loads(live.info_path("cam").read_text())["frame_time"]
    w.write_info(0.0, "no frames from the camera for 6 s")
    info = json.loads(live.info_path("cam").read_text())
    assert info["frame_time"] == first and info["warning"].startswith("no frames")


# --- Stale frame message -----------------------------------------------------------------


def test_read_view_reports_frame_age():
    w = LiveWriter("cam", clock=Clock())
    w.frame(frame(), fps=8.0)
    view = live.read_view("cam")
    assert view.image and view.fps == 8.0 and 0 <= view.frame_age < 2
    assert live.frame_message(view.frame_age, None) is None


def test_old_frame_says_not_sending(monkeypatch):
    w = LiveWriter("cam", clock=Clock())
    w.frame(frame())
    real_time = time.time
    monkeypatch.setattr(live.time, "time", lambda: real_time() + 6)
    view = live.read_view("cam")
    assert view.frame_age > config.LIVE_STALE_SECONDS
    assert live.frame_message(view.frame_age, view.waiting_for) == live.NOT_SENDING == "camera not sending frames"


@pytest.mark.parametrize("age, waiting_for, expected", [
    (0.4, None, None),
    (4.9, None, None),
    (5.1, None, live.NOT_SENDING),
    (None, None, live.WAITING),  # still loading models
    (None, 2, live.WAITING),  # camera loop just started
    (None, 6, live.NOT_SENDING),  # camera loop running a while without a single frame
])
def test_frame_message(age, waiting_for, expected):
    assert live.frame_message(age, waiting_for) == expected


def test_waiting_measured_from_camera_loop_start(monkeypatch):
    w = LiveWriter("cam", clock=Clock())
    w.start()
    assert live.frame_message(*_age_and_wait()) == live.WAITING
    real_time = time.time
    monkeypatch.setattr(live.time, "time", lambda: real_time() + 6)
    assert live.frame_message(*_age_and_wait()) == live.NOT_SENDING
    w.frame(frame())
    monkeypatch.setattr(live.time, "time", real_time)
    assert live.frame_message(*_age_and_wait()) is None


def _age_and_wait():
    view = live.read_view("cam")
    return view.frame_age, view.waiting_for


def test_missing_files_mean_no_frame():
    view = live.read_view("nothing")
    assert view.image is None and view.frame_age is None and view.decisions == []


# --- Camera health ------------------------------------------------------------------------


def test_health_warns_when_no_frames():
    clock = Clock()
    h = CameraHealth(clock)
    clock.t += 4
    assert h.update(None) is None
    clock.t += 2
    assert "hasn't sent a frame" in h.update(None)
    assert h.update(frame()) is None  # recovers


def test_health_warns_on_black_frames():
    clock = Clock()
    h = CameraHealth(clock)
    warnings = []
    for _ in range(9):
        clock.t += 1
        warnings.append(h.update(frame(0)))
    assert "only black frames" in warnings[-1]
    assert len(set(w for w in warnings if w)) == 1  # same text every time: logged once, not every second
    assert h.update(frame(120)) is None


# --- Background capture ----------------------------------------------------------------------


class FakeCap:
    def __init__(self, frames=None, block: float = 0.0):
        self.frames = list(frames or [])
        self.block = block
        self.released = False

    def read(self):
        time.sleep(self.block or 0.01)
        if self.frames:
            return True, self.frames.pop(0)
        return False, None

    def release(self):
        self.released = True


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_capture_does_not_block_on_slow_open():
    def slow_open(source):
        time.sleep(1.0)
        return FakeCap([frame()])

    t = time.monotonic()
    cap = LiveCapture(0, opener=slow_open)
    assert time.monotonic() - t < 0.8  # returns without waiting for the 1 s open (slack for a busy machine)
    assert cap.latest() is None and not cap.opened
    seq, img = wait_for(lambda: cap.latest())
    assert seq == 1 and img.shape == (48, 64, 3)
    cap.stop()


def test_capture_keeps_newest_frame():
    cap = LiveCapture(0, opener=lambda s: FakeCap([frame(10), frame(20), frame(30)]), max_failures=1000)
    seq, img = wait_for(lambda: (got := cap.latest()) and got[0] == 3 and got)
    assert int(img.mean()) == 30
    assert cap.latest(after=3) is None
    cap.stop()


def test_capture_reopens_a_webcam_that_fails_to_open(monkeypatch):
    """A webcam that can't be opened (unplugged, held by another app) is tried again with backoff; the worker
    never gets an error that would end it."""
    attempts = []

    def opener(source):
        attempts.append(time.monotonic())
        if len(attempts) < 3:
            raise RuntimeError("busy")
        return FakeCap([frame(40)] * 50)

    log = []
    cap = LiveCapture(0, opener=opener, backoff=(0.05, 0.1), log=log.append)
    seq, img = wait_for(lambda: cap.latest())
    assert int(img.mean()) == 40 and len(attempts) == 3 and cap.error is None
    assert cap.delays == [0.05, 0.1] and cap.reconnects == 2
    assert "could not open source; opening it again in 0.05 s (attempt 1)" in log[0]
    h = cap.health()
    assert h["state"] == "live" and h["reconnects"] == 2 and h["reconnects_today"] == 2
    assert h["last_error"] == "could not open source"
    cap.stop()


def test_capture_reopens_a_lost_webcam():
    """Read failures in a row (USB reset) release the camera and open it again; frames continue."""
    caps = [FakeCap([frame(10)]), FakeCap([frame(20)] * 200)]
    opened = []

    def opener(source):
        opened.append(caps[len(opened)])
        return opened[-1]

    cap = LiveCapture(0, opener=opener, max_failures=3, backoff=(0.05,))
    wait_for(lambda: (got := cap.latest()) and int(got[1].mean()) == 20)
    assert caps[0].released and cap.error is None and cap.last_error == "lost the camera"
    assert cap.delays == [0.05]  # it had delivered a frame: the backoff starts again from the shortest pause
    assert cap.state == "live"
    cap.stop()
    wait_for(lambda: caps[1].released)
    assert cap.state == "stopped"


def test_is_live():
    assert is_live(0) and is_live("rtsp://cam/stream") and is_live("http://cam/mjpg")
    assert not is_live("clip.mp4") and not is_live(r"C:\videos\exit.mp4")


def test_worker_shares_full_frames_only_while_the_wizard_asks(tmp_path):
    clock = [100.0]
    w = live.LiveWriter("gate", live_dir=tmp_path, clock=lambda: clock[0], background=False)
    frame = np.full((1440, 2560, 3), 90, np.uint8)
    assert not w.full_frame(frame) and not live.full_path("gate", tmp_path).exists()  # nobody asked
    live.mark_enrolling("gate", tmp_path)
    clock[0] += 1
    assert w.full_frame(frame)
    shared = cv2.imread(str(live.full_path("gate", tmp_path)))
    assert shared.shape == frame.shape  # raw and full resolution, no boxes drawn
    clock[0] += 0.05
    assert not w.full_frame(frame)  # at most ENROLL_FEED_FPS a second
    clock[0] += 1 / config.ENROLL_FEED_FPS
    assert w.full_frame(frame)
    live.enroll_path("gate", tmp_path).unlink()
    clock[0] += 1
    assert not w.full_frame(frame) and not live.full_path("gate", tmp_path).exists()  # stale: the frame goes


def test_background_sharing_writes_the_newest_frame(tmp_path):
    w = live.LiveWriter("gate", live_dir=tmp_path)
    live.mark_enrolling("gate", tmp_path)
    assert w.full_frame(np.full((48, 64, 3), 200, np.uint8))
    deadline = time.monotonic() + 5
    while not live.full_path("gate", tmp_path).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cv2.imread(str(live.full_path("gate", tmp_path))).shape == (48, 64, 3)
    live.stop_enrolling("gate", tmp_path)
    assert not live.full_path("gate", tmp_path).exists()
