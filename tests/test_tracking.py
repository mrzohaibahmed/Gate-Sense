import numpy as np

from app.detector import Detection
from app.gallery import Match
from app.tracker import FaceTracker, Sample, TrackState


def det_at(x: float) -> Detection:
    return Detection(np.array([x, 100, x + 100, 220], np.float32), np.full((5, 2), x, np.float32), 0.9)


def test_tracker_keeps_id_and_landmarks():
    tr = FaceTracker(fps=8)
    ids = []
    for f in range(5):
        out = tr.update([det_at(100 + 5 * f), det_at(400)])
        ids.append(sorted(tid for tid, _ in out))
        for _, d in out:
            assert d.kps[0, 0] == d.bbox[0]  # landmarks stay with their own box
    assert all(i == ids[0] for i in ids) and len(ids[0]) == 2


def test_tracker_new_unconfirmed_track_returns_empty():
    tr = FaceTracker(fps=8)
    assert len(tr.update([det_at(100)])) == 1  # first frame activates immediately
    for _ in range(30):  # long gap: the track is dropped
        assert tr.update([]) == []
    assert tr.update([det_at(400)]) == []  # new track not confirmed yet (used to raise KeyError)
    assert len(tr.update([det_at(400)])) == 1


def sample(quality: float, name: str | None, score: float = 0.6) -> Sample:
    m = Match(name, name or "someone", score, None, None, "match" if name else "below threshold")
    return Sample(quality, m, np.zeros((4, 4, 3), np.uint8))


def test_keeps_only_best_frames():
    tr = TrackState(1)
    for q in [5, 1, 4, 2, 3, 6]:
        tr.add(sample(q, "waqas"), keep=5)
    assert sorted(s.quality for s in tr.samples) == [2, 3, 4, 5, 6]


def test_wants_only_frames_better_than_worst_kept():
    tr = TrackState(1)
    for q in [3, 4, 5]:
        tr.add(sample(q, None), keep=3)
    assert not tr.wants(2, keep=3)
    assert tr.wants(3.5, keep=3)


def test_decided_track_wants_nothing():
    tr = TrackState(1, decided="waqas")
    assert not tr.wants(100)


def test_vote_needs_min_agree():
    tr = TrackState(1)
    for name in ["waqas", "waqas", None, "ali"]:
        tr.add(sample(1, name))
    assert tr.vote(min_agree=3) is None
    tr.add(sample(1, "waqas"))
    assert tr.vote(min_agree=3) == ("waqas", 3)


def test_unknown_frames_never_win():
    tr = TrackState(1)
    for _ in range(5):
        tr.add(sample(1, None))
    assert tr.vote(min_agree=3) is None


def test_best_sample_is_highest_score_for_name():
    tr = TrackState(1)
    tr.add(sample(9, "waqas", score=0.55))
    tr.add(sample(1, "waqas", score=0.71))
    tr.add(sample(5, "ali", score=0.90))
    assert tr.best_sample("waqas").match.score == 0.71
