"""app.probe_camera with a fake frame source and the fake detector (no camera, no models)."""

import time

import cv2
import numpy as np
import pytest

from app import config, probe_camera
from app.detector import Detection
from app.frame_source import FrameSource, fourcc_text
from tests.fakes import FakeDetector, photo

URL = "rtsp://admin:S3cretPw@192.168.1.64:554/Streaming/Channels/101"
H264 = cv2.VideoWriter_fourcc(*"h264")


class FakeCap:
    """A stream of the given frames, repeated, reporting a codec and fps like an RTSP capture."""

    def __init__(self, frames, fourcc=H264, fps=25.0):
        self.frames, self.i = frames, 0
        self.fourcc, self.fps = fourcc, fps

    def isOpened(self):
        return True

    def read(self):
        time.sleep(0.01)
        frame = self.frames[self.i % len(self.frames)]
        self.i += 1
        return True, frame

    def get(self, prop):
        return {cv2.CAP_PROP_FOURCC: self.fourcc, cv2.CAP_PROP_FPS: self.fps}.get(prop, 0.0)

    def release(self):
        pass


def source_of(*frames, opener=None, **kw) -> FrameSource:
    return FrameSource(URL, opener=opener or (lambda url: FakeCap(list(frames), **kw)), log=lambda *_: None,
                       wait=lambda s: False, poll=0.01)


def run(frames, out, detector=None, seconds=0.6, label="gate"):
    return probe_camera.probe(URL, label, seconds, detector or FakeDetector(), frames=frames, out=out.append)


def test_reports_codec_resolution_fps_and_faces():
    out = []
    r = run(source_of(photo("good")), out)
    assert r.codec == "h264" and (r.width, r.height) == (320, 320) and r.reported_fps == 25.0
    assert r.frames > 5 and r.input_fps > 10 and r.detect_ms >= 0 and r.read_ms > 0
    assert r.faces and all(f[1] == f[2] == 200 and f[3] == pytest.approx(0.9) for f in r.faces)
    assert r.median_width == 200 and not r.too_small
    text = "\n".join(out)
    assert "Codec      h264" in text and "Resolution 320x320" in text and "camera reports 25.0" in text
    assert "face width  200 px (camera), 200 px (processed)  score 0.90" in text
    assert "median width 200 px" in text and "WARNING" not in text


def test_small_faces_warn():
    out = []
    r = run(source_of(photo("small")), out)  # the fake detector sees a 50 px face
    assert r.median_width == 50 and r.too_small
    assert f"WARNING: {probe_camera.SMALL_FACES}" in out
    assert "faces too small: zoom in, move the camera closer or use 1920x1080" in "\n".join(out)


def test_median_uses_all_faces():
    out = []
    r = run(source_of(photo("good"), photo("two")), out)  # 200 px faces and pairs of 120 px faces
    widths = sorted(f[2] for f in r.faces)
    assert set(widths) == {120, 200} and r.median_width == pytest.approx(np.median(widths))


def test_no_faces():
    out = []
    r = run(source_of(photo("none")), out)
    assert r.faces == [] and r.median_width is None and not r.too_small
    assert any("none detected" in line for line in out)


def test_widths_in_camera_and_processed_pixels():
    class Fixed:
        def detect(self, img):  # one 100 px face in the processed (resized) frame
            return [Detection(np.array([100, 100, 200, 200], np.float32), np.zeros((5, 2), np.float32), 0.8)]

    out = []
    big = np.zeros((720, 1280, 3), np.uint8)
    r = run(source_of(big), out, detector=Fixed())
    assert r.faces[0][1:3] == (200, 100)  # 1280 px camera frame is processed at 640 px
    assert r.median_width == 100  # the 80 px check uses the processed frame, like the quality filter


def test_snapshot_has_face_boxes():
    out = []
    r = run(source_of(photo("good")), out, label="gate")
    assert r.snapshot.parent == config.DATA_DIR / "probe" and r.snapshot.name.startswith("gate_")
    snap = cv2.imread(str(r.snapshot))
    assert snap.shape == (320, 320, 3)
    green = (snap[:, :, 1] > 150) & (snap[:, :, 0] < 80) & (snap[:, :, 2] < 80)
    assert green[60, 60:260].mean() > 0.5  # the box's top edge
    assert f"Snapshot   {r.snapshot}" in out


def test_passwords_masked_everywhere():
    attempts = []

    def opener(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError(f"cannot open {url}")  # logged by FrameSource's reconnect
        return FakeCap([photo("good")])

    out = []
    frames = FrameSource(URL, opener=opener, log=lambda line: out.append(probe_camera.mask_source(line)),
                         wait=lambda s: False, poll=0.01)
    probe_camera.probe(URL, "gate", 0.6, FakeDetector(), frames=frames, out=out.append)
    text = "\n".join(out)
    assert "could not connect" in text and "S3cretPw" not in text and "admin:***@192.168.1.64" in text


def test_no_frames():
    out = []
    r = run(source_of(opener=lambda url: (_ for _ in ()).throw(RuntimeError("down"))), out, seconds=0.3)
    assert r.frames == 0 and r.snapshot is None
    assert any("No frames received" in line for line in out)


@pytest.mark.parametrize("target, label", [
    ("entrance", "entrance"),
    ("0", "webcam0"),
    (URL, "rtsp_192.168.1.64"),
    (r"D:\videos\gate walk.mp4", "file_gate_walk"),
])
def test_resolve(target, label):
    got_label, source = probe_camera.resolve(target)
    assert got_label == label and "S3cretPw" not in got_label
    if target == "0":
        assert source == 0


def test_bogus_reported_fps_is_ignored():
    out = []
    r = run(source_of(photo("good"), fps=-1.0), out)
    assert r.reported_fps is None and not any("camera reports" in line for line in out)


def test_fourcc_text():
    assert fourcc_text(H264) == "h264" and fourcc_text(cv2.VideoWriter_fourcc(*"MJPG")) == "MJPG"
    assert fourcc_text(0) == "unknown" and fourcc_text(-1) == "unknown" and fourcc_text(0x01020304) == "unknown"


def test_main_on_a_video_file(tmp_path, monkeypatch, capsys):
    """End to end through main(): a real video file opened by FrameSource with the normal opener."""
    clip = tmp_path / "walk.avi"
    w = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"MJPG"), 25, (320, 320))
    for i in range(50):
        w.write(photo("good", seed=i))
    w.release()
    monkeypatch.setattr("app.detector.Detector", FakeDetector)
    assert probe_camera.main([str(clip), "--seconds", "1"]) == 0
    out = capsys.readouterr().out
    assert "Codec      MJPG" in out and "Resolution 320x320" in out and "median width 200 px" in out
    assert list((config.DATA_DIR / "probe").glob("file_walk_*.jpg"))
