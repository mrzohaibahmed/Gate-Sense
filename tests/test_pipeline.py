"""recognize.py's Pipeline reports every decision to the live view: skip, unknown (below threshold / low margin),
match with vote count, event and cool-down. Uses the fake detector and controlled embeddings."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from app.attendance import Cooldown
from app.gallery import Gallery
from app.live import LiveWriter
from tests.fakes import FakeDetector, photo

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

    def record(self, stamp, now, track, name, votes):
        if not self.cooldown.allow(name, "entrance", now):
            return "cool-down", "IN already recorded"
        return "event", f"IN saved (votes {votes})"


def run(vector, kind="good", frames=4, pipeline=None, start=0.0):
    gallery = Gallery.from_people({"waqas": [unit(0)], "ali": [unit(1)]})
    p = pipeline or recognize.Pipeline(gallery, FakeDetector(), FixedRecognizer(vector), StubRecorder(),
                                       LiveWriter("entrance"))
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
