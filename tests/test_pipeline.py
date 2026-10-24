"""recognize.py's Pipeline reports every decision to the live view: skip, unknown (below threshold / low margin),
match with vote count, event and cool-down; liveness (spoof skips, events wait for live frames). Uses the fake
detector and liveness and controlled embeddings.
Also: detection on the FRAME_WIDTH copy, quality/embedding/snapshot on the full frame, and a worker run on
a 1920x1080 clip."""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from app import config, db, workers
from app.attendance import Cooldown
from app.detector import Detection
from app.gallery import Gallery
from app.live import LiveWriter
from app.tracker import TrackState
from tests.fakes import FRONTAL_112, FakeDetector, FakeLiveness, photo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import recognize  # noqa: E402

DIM = 512


def unit(i: int) -> np.ndarray:
    v = np.zeros(DIM, np.float32)
    v[i] = 1
    return v


class FixedRecognizer:
    def __init__(self, vector):
        self.vector = vector

    def embed(self, img, det):
        return self.vector


class StubRecorder:
    """Recorder without a database: the real cool-down, a fake save."""

    conn = None

    def __init__(self):
        self.cooldown = Cooldown(300)
        self.spoofs = []

    def save_spoof(self, now, track_id, score, image):
        self.spoofs.append((track_id, score, image.shape))
        return f"snapshots/2026-10-20/spoof/{track_id}.jpg"

    def record(self, stamp, now, track, name, votes):
        if not self.cooldown.allow(name, "entrance", now):
            return "cool-down", "IN already recorded"
        return "event", f"IN saved (votes {votes})"


def run(vector, kind="good", frames=4, pipeline=None, start=0.0, liveness=None):
    gallery = Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]})
    p = pipeline or recognize.Pipeline(gallery, FakeDetector(), FixedRecognizer(vector), StubRecorder(),
                                       LiveWriter("entrance"), liveness=liveness)
    p.recognizer = FixedRecognizer(vector)
    t0 = datetime(2026, 10, 20, 9, 0)
    for i in range(frames):
        t = start + i * 0.125
        img = p.process(photo(kind, seed=i), t, t0 + timedelta(seconds=t), f"t={t:.2f}")
        assert img.shape == (320, 320, 3)
    return p, [(d["kind"], d["name"], d["detail"]) for d in p.live.decisions]


def test_match_votes_then_event():
    _, decisions = run(unit(0))
    assert decisions[:4] == [
        ("match", "waqas", "vote 1/3"),
        ("match", "waqas", "vote 2/3"),
        ("match", "waqas", "vote 3/3"),
        ("event", "waqas", "IN saved (votes 3)"),
    ]


def test_cooldown_reported():
    p, _ = run(unit(0))
    run(unit(0), kind="none", frames=20, pipeline=p, start=1)  # the person leaves: the track is dropped
    p.live.decisions.clear()
    p, decisions = run(unit(0), frames=6, pipeline=p, start=5)  # and comes back a few seconds later
    assert decisions[-1] == ("cool-down", "waqas", "IN already recorded")


def test_below_threshold_reported():
    _, decisions = run(0.3 * unit(0) + np.sqrt(1 - 0.09) * unit(5))
    assert decisions[0] == ("unknown", "waqas", "below threshold")


def test_low_margin_reported():
    _, decisions = run((unit(0) + unit(1)) / np.sqrt(2))
    assert decisions[0][0] == "unknown" and decisions[0][2] == "low margin"


def test_quality_skip_reported():
    _, decisions = run(unit(0), kind="blurry")
    assert decisions[0][0] == "skip" and decisions[0][2].startswith("blurry (var")


def test_mirror_box():
    assert recognize.mirror_box((10, 5, 60, 40), 320) == (260, 5, 310, 40)


def test_mirrored_view_flips_only_the_display():
    gallery = Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]})
    plain = recognize.Pipeline(gallery, FakeDetector(), FixedRecognizer(unit(0)), StubRecorder(),
                               LiveWriter("plain"))
    mirrored = recognize.Pipeline(gallery, FakeDetector(), FixedRecognizer(unit(0)), StubRecorder(),
                                  LiveWriter("mirrored"), mirror=True)
    src = photo("good")
    when = datetime(2026, 10, 20, 9, 0)
    a = plain.process(src, 0.0, when, "t=0")
    b = mirrored.process(src, 0.0, when, "t=0")
    assert np.array_equal(src, photo("good"))  # input untouched
    # Away from the box and its label, the mirrored view is the input flipped left-right.
    assert np.array_equal(b[280:, :], src[280:, ::-1]) and np.array_equal(a[280:, :], src[280:, :])
    # Recognition is the same either way: the original frame is used.
    assert [d["kind"] for d in plain.live.decisions] == [d["kind"] for d in mirrored.live.decisions] == ["match"]
    assert plain.live.decisions[0]["score"] == mirrored.live.decisions[0]["score"]


def test_frame_is_annotated_not_the_input():
    p, _ = run(unit(0), frames=1)
    src = photo("good")
    out = p.process(src, 1.0, datetime(2026, 10, 20, 9, 0, 1), "t=1")
    assert out is not src and np.array_equal(src, photo("good"))  # boxes drawn on a copy
    assert not np.array_equal(out, src)


# --- Detection on the 640 px copy, everything after it on the full frame ----------------------


class SmallFaceDetector:
    """One face, `size` px wide in whatever (resized) frame it is given; records the frame sizes it saw."""

    def __init__(self, size: int = 50):
        self.size, self.sizes, self.dets = size, [], []

    def detect(self, img, det_thresh=None):
        self.sizes.append(img.shape[:2])
        x, y, n = 200, 100, self.size
        det = Detection(np.array([x, y, x + n, y + n], np.float32), FRONTAL_112 * (n / 112) + [x, y], 0.9)
        self.dets.append(det)
        return [det]


class RecordingRecognizer:
    def __init__(self, vector):
        self.vector, self.calls = vector, []

    def embed(self, img, det):
        self.calls.append((img, det))
        return self.vector


def full_hd(seed: int = 0) -> np.ndarray:
    """A sharp 1920x1080 frame (4x4 px noise blocks, which also compress well in a clip)."""
    small = np.random.default_rng(seed).integers(0, 256, (270, 480, 3), dtype=np.uint8)
    return cv2.resize(small, (1920, 1080), interpolation=cv2.INTER_NEAREST)


def test_quality_embedding_and_snapshot_use_the_full_frame(capsys):
    detector, recognizer = SmallFaceDetector(), RecordingRecognizer(unit(0))
    gallery = Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]})
    p = recognize.Pipeline(gallery, detector, recognizer, StubRecorder(), LiveWriter("gate"))
    frame = full_hd()
    view = p.process(frame, 0.0, datetime(2026, 10, 20, 9, 0), "t=0")

    assert detector.sizes == [(360, 640)]  # detection on the FRAME_WIDTH copy, as before
    assert view.shape == (360, 640, 3)  # the live view stays the detection-size image
    img, det = recognizer.calls[0]
    assert img is frame  # ArcFace aligns from the full-resolution frame...
    assert np.allclose(det.bbox, [600, 300, 750, 450]) and np.allclose(det.kps, detector.dets[0].kps * 3)
    # ...after the quality filter passed a face that is 50 px in the copy but 150 camera px (the old path
    # said "too small (50px < 80)" here).
    d = p.live.decisions[0]
    assert (d["kind"], d["name"], d["face_px"]) == ("match", "waqas", 150)
    snapshot = p.tracks[next(iter(p.tracks))].samples[0].snapshot
    assert max(snapshot.shape[:2]) == config.SNAPSHOT_SIZE  # 210 px full-res crop shrunk, not a 70 px one
    assert "frame size 1920x1080: detection at 640x360 (scale 3.00)" in capsys.readouterr().out


def test_too_small_in_camera_pixels_is_reported_with_its_width():
    detector = SmallFaceDetector()
    p = recognize.Pipeline(Gallery.from_people({"waqas": [unit(0)]}), detector, RecordingRecognizer(unit(0)),
                           StubRecorder(), LiveWriter("gate"))
    frame = cv2.resize(full_hd(), (1280, 720), interpolation=cv2.INTER_NEAREST)  # 50 px at 640 = 100 camera px
    p.process(frame, 0.0, datetime(2026, 10, 20, 9, 0), "t=0")
    assert p.live.decisions[0]["face_px"] == 100 and p.live.decisions[0]["kind"] == "match"


def test_640_frame_is_used_exactly_as_before(capsys):
    """A webcam-sized frame isn't resized: the very frame and detection go to the quality filter and ArcFace."""
    detector, recognizer = SmallFaceDetector(100), RecordingRecognizer(unit(0))
    p = recognize.Pipeline(Gallery.from_people({"waqas": [unit(0)]}), detector, recognizer, StubRecorder(),
                           LiveWriter("entrance"))
    frame = full_hd()[:480, :640].copy()
    view = p.process(frame, 0.0, datetime(2026, 10, 20, 9, 0), "t=0")
    img, det = recognizer.calls[0]
    assert img is frame and det is detector.dets[0] and view.shape == frame.shape
    assert p.live.decisions[0]["face_px"] == 100 and p.live.decisions[0]["kind"] == "match"

    small = recognize.Pipeline(Gallery.from_people({"waqas": [unit(0)]}), SmallFaceDetector(50),
                               RecordingRecognizer(unit(0)), StubRecorder(), LiveWriter("entrance"))
    small.process(frame, 0.0, datetime(2026, 10, 20, 9, 0), "t=0")
    d = small.live.decisions[0]  # 50 px is still too small on a 640 px webcam, as before
    assert (d["kind"], d["detail"], d["face_px"]) == ("skip", f"too small (50px < {config.MIN_FACE_PX})", 50)
    assert "frame size 640x480: detection and recognition both at 640x480 (no resize)" in capsys.readouterr().out


def test_worker_run_on_a_1080p_clip(tmp_path, monkeypatch, capsys):
    """recognize.run end to end on a 1920x1080 video file: detection at 640 px, recognition at full size,
    an event saved to the database with a full-resolution snapshot."""
    clip = tmp_path / "gate.avi"
    w = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"MJPG"), 25, (1920, 1080))
    for i in range(15):  # 25 fps at PROCESS_FPS 8 = every 3rd frame: 5 processed
        w.write(full_hd(seed=i))
    w.release()

    Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]}).save()
    conn = db.connect()
    db.sync_persons(conn, ["waqas", "ali"])
    conn.close()
    detector, recognizer = SmallFaceDetector(), RecordingRecognizer(unit(0))
    monkeypatch.setattr(recognize, "Detector", lambda: detector)
    monkeypatch.setattr(recognize, "Recognizer", lambda: recognizer)
    monkeypatch.setattr(recognize, "providers_in_use", lambda: {"detection": ["CPUExecutionProvider"]})
    checker = FakeLiveness(0.97)
    monkeypatch.setattr(recognize, "Liveness", lambda: checker)

    cam = {"name": "gate", "source": str(clip), "direction": "IN"}
    args = argparse.Namespace(no_db=False, no_window=True)
    runtime, live = workers.WorkerRuntime("gate", str(clip)), LiveWriter("gate")
    try:
        assert recognize.run(args, cam, str(clip), runtime, live) == "end of video"
    finally:
        runtime.close("end of video")

    assert detector.sizes == [(360, 640)] * 5
    assert {img.shape for img, _ in recognizer.calls} == {(1080, 1920, 3)}
    assert all(np.allclose(det.bbox, [600, 300, 750, 450]) for _, det in recognizer.calls)
    # The liveness check also gets the full-resolution frame and the face in camera pixels, before each embedding.
    assert len(checker.calls) == 3 and {shape for shape, _ in checker.calls} == {(1080, 1920, 3)}
    assert all(np.allclose(bbox, [600, 300, 750, 450]) for _, bbox in checker.calls)
    kinds = [(d["kind"], d["face_px"], d["liveness"]) for d in live.decisions]
    assert kinds[:4] == [("match", 150, 0.97), ("match", 150, 0.97), ("match", 150, 0.97), ("event", None, None)]
    out = capsys.readouterr().out
    assert "frame size 1920x1080: detection at 640x360 (scale 3.00)" in out
    assert "Liveness on: live score >= 0.7, 3 live frames per track" in out
    assert "too small" not in out

    conn = db.connect()
    events = db.recent_events(conn)
    conn.close()
    assert [(e["employee_code"], e["direction"]) for e in events] == [("waqas", "IN")]
    snap = cv2.imread(str(config.DATA_DIR / events[0]["snapshot_path"]))
    assert max(snap.shape[:2]) == config.SNAPSHOT_SIZE  # cropped from the 1920 px frame


# --- Liveness (anti-spoofing) ----------------------------------------------------------------------------------


def test_spoof_frames_never_vote(capsys):
    """A phone or a photo: every frame is skipped as a spoof, nothing is embedded or recorded, one review snapshot
    and one warning for the track."""
    p, decisions = run(unit(0), frames=6, liveness=FakeLiveness(0.12))
    assert decisions == [("skip", None, "spoof (0.12)")] * 6
    assert [d["liveness"] for d in p.live.decisions] == [0.12] * 6 and p.live.decisions[0]["face_px"] == 200
    track = next(iter(p.tracks.values()))
    assert track.samples == [] and track.decided is None and track.spoof_frames == 6
    assert p.recorder.spoofs == [(track.track_id, 0.12, (320, 320, 3))]  # once per track, with the background
    out = capsys.readouterr().out
    assert out.count("WARNING: track") == 1 and "looks like a phone screen or a printed photo" in out
    assert "snapshots/2026-10-20/spoof/" in out and out.count("spoof (live 0.120 < 0.7)") == 6
    assert p.stats["skip: spoof"] == 6


def test_an_event_needs_three_live_frames():
    """Real frames among spoofs: only the live ones vote, and the event comes with the third live one."""
    p, decisions = run(unit(0), frames=5, liveness=FakeLiveness(0.95, 0.30, 0.91, 0.40, 0.88))
    assert decisions == [
        ("match", "waqas", "vote 1/3, live 1/3"),
        ("skip", None, "spoof (0.30)"),
        ("match", "waqas", "vote 2/3, live 2/3, 1 spoof"),
        ("skip", None, "spoof (0.40)"),
        ("match", "waqas", "vote 3/3, live 3/3, 2 spoof"),
        ("event", "waqas", "IN saved (votes 3)"),
    ]


def test_a_won_vote_waits_for_live_frames(monkeypatch, capsys):
    """VOTE_MIN_AGREE 1, one kept frame: the vote is won at once, but the event waits for LIVENESS_MIN_FRAMES live
    frames, which are checked even though the track wants no more embeddings."""
    monkeypatch.setattr(config, "VOTE_MIN_AGREE", 1)
    monkeypatch.setattr(TrackState.vote, "__defaults__", (1,))  # its defaults are read when it's imported
    monkeypatch.setattr(TrackState.wants, "__defaults__", (1,))
    checker = FakeLiveness(0.9)
    gallery = Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]})
    recognizer = RecordingRecognizer(unit(0))
    p = recognize.Pipeline(gallery, FakeDetector(), recognizer, StubRecorder(), LiveWriter("entrance"),
                           liveness=checker)
    when = datetime(2026, 10, 20, 9, 0)
    for i in range(2):
        p.process(photo("good", seed=i), i * 0.125, when, f"t={i}")
    assert [d["kind"] for d in p.live.decisions] == ["match"]  # vote won, no event yet
    assert "vote won, waiting for liveness (live 1/3)" in capsys.readouterr().out
    p.process(photo("good", seed=2), 0.25, when, "t=2")
    assert [d["kind"] for d in p.live.decisions] == ["match", "event"]
    assert len(recognizer.calls) == 1 and len(checker.calls) == 3


def test_a_track_that_leaves_reports_its_liveness():
    p, _ = run(unit(0), frames=2, liveness=FakeLiveness(0.95, 0.1))
    run(unit(0), kind="none", frames=20, pipeline=p, start=1)  # the face is gone: the track closes
    closed = p.live.decisions[-1]
    assert closed["kind"] == "closed" and closed["detail"] == "left without a decision (waqas 1; live 1/3, 1 spoof)"


def test_without_liveness_nothing_changes():
    p, decisions = run(unit(0))
    assert decisions[2] == ("match", "waqas", "vote 3/3") and decisions[3][0] == "event"
    assert all(d["liveness"] is None for d in p.live.decisions) and not p.liveness_ms


def test_liveness_time_is_measured():
    p, _ = run(unit(0), frames=3, liveness=FakeLiveness(0.95))
    assert p.ms_per_liveness() == 1.0 and "liveness 1.0 ms/face" in p.stats_line()


def test_a_spoof_snapshot_is_saved_for_review():
    """The real Recorder writes it under snapshots/<day>/spoof/ (removed with its day by the nightly cleanup)."""
    recorder = recognize.Recorder({"name": "gate", "direction": "IN"}, 0, use_db=True)
    try:
        rel = recorder.save_spoof(datetime(2026, 10, 20, 9, 5, 7), 12, 0.123, photo("good"))
    finally:
        recorder.conn.close()
    assert rel == "snapshots/2026-10-20/spoof/090507_track12_gate_0.12.jpg"
    assert cv2.imread(str(config.DATA_DIR / rel)).shape == (320, 320, 3)
    assert recognize.Recorder({"name": "gate", "direction": "IN"}, 0, use_db=False).save_spoof(
        datetime(2026, 10, 20), 1, 0.1, photo("good")) is None  # --no-db writes nothing


def test_worker_refuses_to_run_without_the_liveness_models(tmp_path, monkeypatch, capsys):
    """Liveness on but its models can't be had (no download in tests): the worker stops instead of recording
    without it."""
    Gallery.from_people({"waqas": [unit(0)]}).save()
    monkeypatch.setattr(recognize, "Detector", FakeDetector)
    monkeypatch.setattr(recognize, "Recognizer", lambda: FixedRecognizer(unit(0)))
    monkeypatch.setattr(recognize, "providers_in_use", lambda: {})
    cam = {"name": "gate", "source": str(tmp_path / "clip.avi"), "direction": "IN"}
    args = argparse.Namespace(no_db=True, no_window=True)
    runtime, live = workers.WorkerRuntime("gate", cam["source"]), LiveWriter("gate")
    try:
        reason = recognize.run(args, cam, cam["source"], runtime, live)
    finally:
        runtime.close("test")
    assert reason == "liveness model missing" and reason in recognize.FAILURES
    assert "the liveness check is on (LIVENESS_ENABLED) but can't run" in capsys.readouterr().out
    monkeypatch.setattr(config, "LIVENESS_ENABLED", False)  # off: it runs (and says so)
    runtime = workers.WorkerRuntime("gate", cam["source"])
    try:
        assert recognize.run(args, cam, cam["source"], runtime, live) == "video file not found"
    finally:
        runtime.close("test")
    assert "Liveness OFF" in capsys.readouterr().out
