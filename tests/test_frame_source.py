"""FrameSource (RTSP) with fake capture objects: newest frame only, stale timeout, reconnect with backoff,
and webcams / files keeping their old path. No real camera or network."""

import os
import threading
import time

import numpy as np
import pytest

from app import config, frame_source
from app.camera import LiveCapture
from app.frame_source import FrameSource

URL = "rtsp://admin:S3cretPw@192.168.1.64:554/Streaming/Channels/101"


def img(n: int) -> np.ndarray:
    return np.full((36, 64, 3), n % 256, np.uint8)


class FakeCap:
    """Stands in for cv2.VideoCapture. `frames`: list of images, then behaviour after they run out."""

    def __init__(self, frames=(), then="fail", delay=0.0, opened=True, hang=30.0):
        self.frames = list(frames)
        self.then = then  # "fail" (read returns False) or "hang" (read blocks, like a frozen stream)
        self.delay = delay
        self.opened = opened
        self.hang = hang  # a frozen read returns after this long (FFmpeg's read timeout)
        self.released = threading.Event()
        self.reads = 0

    def isOpened(self):
        return self.opened

    def read(self):
        self.reads += 1
        if self.delay:
            time.sleep(self.delay)
        if self.frames:
            return True, self.frames.pop(0)
        if self.then == "hang":
            time.sleep(self.hang)
        return False, None

    def release(self):
        self.released.set()


class Opener:
    """Returns the prepared captures (or raises the prepared exceptions) in order, then the last one forever."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, source):
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result() if callable(result) else result


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.01)
    raise AssertionError("timed out")


@pytest.fixture
def logs():
    return []


def make(opener, logs, **kw) -> FrameSource:
    kw.setdefault("stale_after", 0.3)
    kw.setdefault("connect_grace", 0.5)
    kw.setdefault("wait", lambda s: False)  # no real backoff sleeps
    kw.setdefault("poll", 0.01)
    return FrameSource(URL, opener=opener, log=logs.append, **kw)


# --- Newest frame only ---------------------------------------------------------------------


def test_keeps_only_the_newest_frame(logs):
    cap = FakeCap([img(i) for i in range(1, 201)], then="hang")
    src = make(Opener(cap), logs, stale_after=5)
    seq, frame = wait_for(lambda: (got := src.latest()) and got[0] == 200 and got)
    assert int(frame[0, 0, 0]) == 200  # frame 200, not a queued older one
    assert src.latest(after=200) is None  # nothing new: no backlog to replay
    assert src.state == "live" and src.size == (64, 36)
    src.stop()


def test_consumer_skips_frames_it_was_too_slow_for(logs):
    cap = FakeCap([img(i) for i in range(1, 400)], then="hang", delay=0.002)
    src = make(Opener(cap), logs, stale_after=5)
    seen, last = [], 0
    while last < 300:
        got = src.latest(last)
        if got:
            last = got[0]
            seen.append(last)
            time.sleep(0.05)  # a slow recognition step
    assert seen == sorted(seen) and len(seen) < 100  # always newer, most frames skipped
    src.stop()


def test_health_reports_live_state(logs):
    cap = FakeCap([img(i) for i in range(1, 50)], then="hang", delay=0.01)
    src = make(Opener(cap), logs, stale_after=5)
    wait_for(lambda: src.state == "live")
    h = src.health()
    assert h["kind"] == "rtsp" and h["state"] == "live" and (h["width"], h["height"]) == (64, 36)
    assert h["reconnects"] == 0 and h["last_error"] is None and h["live_since"]
    assert time.time() - h["last_frame_at"] < 2
    wait_for(lambda: src.health()["input_fps"] > 10)
    assert any("camera live" in line and "S3cretPw" not in line for line in logs)
    src.stop()


# --- Stale frames and failures -> reconnect ---------------------------------------------------


def test_stale_stream_reconnects(logs):
    first = FakeCap([img(1), img(2)], then="hang", hang=1.0)  # sends two frames, then freezes
    second = FakeCap([img(i) for i in range(10, 60)], then="hang")
    opener = Opener(first, second)
    src = make(opener, logs, stale_after=0.3)
    wait_for(lambda: (got := src.latest()) and int(got[1][0, 0, 0]) >= 10)
    assert opener.calls == 2 and src.reconnects == 1
    assert src.last_error == "no new frame for 0.3 s"
    assert first.released.wait(3)  # the abandoned capture is released as soon as its frozen read returns
    assert any("disconnected" in line for line in logs)
    src.stop()


def test_read_failure_reconnects(logs):
    opener = Opener(FakeCap([img(1)], then="fail"), FakeCap([img(i) for i in range(2, 30)], then="hang"))
    src = make(opener, logs)
    wait_for(lambda: src.reconnects == 1 and src.state == "live")
    assert src.last_error == "stream dropped (read failed)"
    src.stop()


def test_no_frame_after_connecting_counts_as_failure(logs):
    opener = Opener(FakeCap([], then="hang"), FakeCap([img(i) for i in range(30)], then="hang"))
    src = make(opener, logs, connect_grace=0.3)
    wait_for(lambda: src.state == "live")
    assert src.last_error == "no frame within 0.3 s of connecting"
    src.stop()


def test_open_errors_are_masked_and_never_crash(logs):
    opener = Opener(RuntimeError(f"cannot open {URL}"), FakeCap(opened=False),
                    FakeCap([img(i) for i in range(30)], then="hang"))
    src = make(opener, logs)
    wait_for(lambda: src.state == "live")
    assert src.reconnects == 2 and opener.calls == 3
    text = " ".join(logs) + str(src.health())
    assert "S3cretPw" not in text and "admin:***@" in text


# --- Backoff ----------------------------------------------------------------------------------


def test_backoff_doubles_to_30_and_resets_after_success(logs):
    waits = []
    good = FakeCap([img(1)], then="fail")  # connects, sends a frame, then drops
    results = [RuntimeError("down")] * 8 + [good] + [RuntimeError("down")] * 3 + [FakeCap([img(2)], then="hang")]
    opener = Opener(*results)
    src = make(opener, logs, wait=lambda s: waits.append(s) or False, stale_after=5)
    wait_for(lambda: opener.calls >= len(results) and src.state == "live")
    src.stop()
    assert waits[:8] == [1, 2, 4, 8, 16, 30, 30, 30]  # capped at 30 s, forever
    assert waits[8:12] == [1, 2, 4, 8]  # the good connection reset the backoff: its drop starts again at 1 s
    assert src.reconnects == 12


def test_backoff_uses_config(monkeypatch, logs):
    monkeypatch.setattr(config, "RTSP_RECONNECT_BACKOFF", (1, 2, 4, 8, 16, 30))
    src = FrameSource(URL, opener=Opener(RuntimeError("down")), log=logs.append, wait=lambda s: True)
    wait_for(lambda: src.state == "stopped")  # wait() returning True = stopping
    assert src.delays == [1]


def test_stop_interrupts_backoff(logs):
    src = FrameSource(URL, opener=Opener(RuntimeError("down")), log=logs.append, backoff=(30,), poll=0.01)
    wait_for(lambda: src.state == "reconnecting")
    t = time.monotonic()
    src.stop()
    assert time.monotonic() - t < 1 and src.state == "stopped"  # not stuck in the 30 s wait


# --- Opening RTSP with FFmpeg --------------------------------------------------------------------


def test_open_rtsp_uses_ffmpeg_tcp_and_timeouts(monkeypatch):
    calls = []
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    monkeypatch.setattr(frame_source.cv2, "VideoCapture", lambda *a: calls.append((a, os.environ.get(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS"))) or FakeCap())
    frame_source.open_rtsp(URL)
    (args, options), = calls
    assert args[0] == URL and args[1] == frame_source.cv2.CAP_FFMPEG
    assert args[2] == [frame_source.cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, frame_source.cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000]
    assert options == "rtsp_transport;tcp|timeout;5000000|stimeout;5000000"  # set before the capture is created


def test_existing_ffmpeg_options_are_respected(monkeypatch):
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;udp")
    monkeypatch.setattr(frame_source.cv2, "VideoCapture", lambda *a: FakeCap())
    frame_source.open_rtsp(URL)
    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "rtsp_transport;udp"


# --- Webcams and files keep the old path ------------------------------------------------------------


@pytest.mark.parametrize("source, kind", [
    (0, "webcam"), (1, "webcam"), (URL, "rtsp"), ("RTSP://cam/1", "rtsp"), ("http://cam/mjpg", "stream"),
    ("clip.mp4", "file"), (r"D:\videos\exit.mp4", "file"),
])
def test_kind(source, kind):
    assert frame_source.kind(source) == kind


def test_webcam_still_uses_live_capture(monkeypatch):
    monkeypatch.setattr("app.camera.open_capture", lambda s: FakeCap([img(1)], then="hang"))
    cap = frame_source.open_live(0)
    assert type(cap) is LiveCapture  # the webcam reader (reopens with its own backoff)
    wait_for(lambda: cap.latest())
    assert cap.health()["kind"] == "webcam" and cap.health()["state"] == "live"
    cap.stop()


def test_files_are_not_live():
    assert frame_source.open_live("clip.mp4") is None  # the worker reads files in order itself


def test_rtsp_gets_frame_source(monkeypatch):
    monkeypatch.setattr(frame_source, "open_rtsp", lambda url: FakeCap([img(1)], then="hang"))
    src = frame_source.open_live(URL, log=lambda *_: None)
    assert isinstance(src, FrameSource)
    wait_for(lambda: src.state == "live")
    src.stop()
