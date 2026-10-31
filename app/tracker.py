"""Face tracking (ByteTrack from the MIT-licensed supervision package) and per-track voting.

ByteTrack gives each face a track ID while the person walks past. For every
track only the best BEST_FRAMES_PER_TRACK frames are embedded and kept; a
frame is embedded only if the track has room or the frame beats its worst
kept frame. The track is decided as soon as one person wins VOTE_MIN_AGREE of
the kept frames, and after that it is no longer embedded at all.

With the liveness check on, only frames that pass it are embedded (a spoof never votes), and a won vote is
recorded only once LIVENESS_MIN_FRAMES of the track's frames have passed it.
"""

import warnings
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import supervision as sv

from app import config
from app.detector import Detection
from app.gallery import Match

# supervision 0.30.9 is pinned: it is the last release with ByteTrack (removed in 0.31,
# moved to Roboflow's separate `trackers` package). Silence its deprecation notice.
warnings.filterwarnings("ignore", message=".*ByteTrack.*", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*ByteTrack.*", category=DeprecationWarning)


class FaceTracker:
    def __init__(self, fps: float):
        # ByteTrack scales lost_track_buffer by frame_rate / 30, so give it the 30 fps equivalent.
        self._bt = sv.ByteTrack(
            track_activation_threshold=config.TRACK_ACTIVATION_THRESHOLD,
            lost_track_buffer=max(1, round(config.TRACK_LOST_SECONDS * 30)),
            minimum_matching_threshold=config.TRACK_MATCH_THRESHOLD,
            frame_rate=max(1, round(fps)),
        )

    def update(self, dets: list[Detection]) -> list[tuple[int, Detection]]:
        """Feed one processed frame; return (track_id, detection) for each tracked face."""
        if not dets:
            self._bt.update_with_detections(sv.Detections.empty())
            return []
        svd = sv.Detections(
            xyxy=np.stack([d.bbox for d in dets]).astype(np.float32),
            confidence=np.array([d.score for d in dets], dtype=np.float32),
            class_id=np.zeros(len(dets), dtype=int),
            data={"idx": np.arange(len(dets))},
        )
        out = self._bt.update_with_detections(svd)
        if len(out) == 0:  # no confirmed tracks: supervision returns a bare empty result without `data`
            return []
        return [(int(tid), dets[int(i)]) for tid, i in zip(out.tracker_id, out.data["idx"])]


@dataclass
class Sample:
    quality: float
    match: Match
    snapshot: np.ndarray  # small face crop, saved if this sample backs an event
    embedding: np.ndarray | None = None  # the frame's face embedding (memory only: an unknown visitor's is matched
    # against the strangers seen recently, app/visitors.py)


@dataclass
class TrackState:
    track_id: int
    last_seen: float = 0.0
    samples: list[Sample] = field(default_factory=list)
    decided: str | None = None  # person name once the vote is won
    live_frames: int = 0  # frames that passed the liveness check (only those can vote)
    spoof_frames: int = 0  # frames refused as a phone screen or printed photo
    spoof_snapshot: str | None = None  # the review snapshot of its first spoof frame ("" = couldn't be saved)
    waiting_logged: bool = False  # "vote won, waiting for liveness" was logged

    def wants(self, quality: float, keep: int = config.BEST_FRAMES_PER_TRACK) -> bool:
        """Is a frame of this quality worth embedding?"""
        if self.decided:
            return False
        return len(self.samples) < keep or quality > min(s.quality for s in self.samples)

    def add(self, sample: Sample, keep: int = config.BEST_FRAMES_PER_TRACK) -> None:
        """Keep the best `keep` samples by quality."""
        self.samples.append(sample)
        if len(self.samples) > keep:
            self.samples.remove(min(self.samples, key=lambda s: s.quality))

    def vote(self, min_agree: int = config.VOTE_MIN_AGREE) -> tuple[str, int] | None:
        """(name, votes) if one person has at least `min_agree` of the kept samples."""
        counts = Counter(s.match.name for s in self.samples if s.match.name)
        if not counts:
            return None
        name, n = counts.most_common(1)[0]
        return (name, n) if n >= min_agree else None

    def best_sample(self, name: str) -> Sample:
        """Highest-scoring kept sample for `name` (its score and snapshot go into the event)."""
        return max((s for s in self.samples if s.match.name == name), key=lambda s: s.match.score)

    def votes_summary(self) -> str:
        counts = Counter(s.match.name or "unknown" for s in self.samples)
        return ", ".join(f"{name} {n}" for name, n in counts.most_common())

    def liveness_summary(self, need: int) -> str:
        spoof = f", {self.spoof_frames} spoof" if self.spoof_frames else ""
        return f"live {min(self.live_frames, need)}/{need}{spoof}"
