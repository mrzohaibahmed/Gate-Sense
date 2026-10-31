"""Unknown visitors: faces that match no enrolled person, recorded for review (the dashboard's Unknown visitors page).

Off by default. The switch (Settings > System > Unknown visitors) is kept in the database (app_settings
"unknown_visitors", app.db.unknown_log_mode); a camera worker reads it every UNKNOWN_SWITCH_SECONDS, so switching it
off stops the recording within seconds, without restarting the cameras.

A tracked face becomes an unknown visitor (`qualifies`) once at least UNKNOWN_MIN_FRAMES of its kept frames are
embedded and every kept frame scores below UNKNOWN_MAX_SCORE against every enrolled person. That is lower than the
match threshold on purpose: a face scoring between the two is probably an enrolled employee seen at a bad angle, not a
stranger, and is left out. With the liveness check on, LIVENESS_MIN_FRAMES of the track's frames must have passed it
too (a photo or a phone screen stays a spoof). The record is written while the person is still in view (so the
guard's alert comes in time) and its last-seen time when the track ends. A track recognised after all (its vote won
later) has its record withdrawn.

Repeats: the worker remembers each recorded stranger's face embedding, in memory only, for UNKNOWN_MERGE_SECONDS. A
new track that matches one (cosine >= UNKNOWN_SAME_PERSON) belongs to the same visit (its `tracks` count goes up)
instead of making a new record, so someone waiting at the gate is one record, not one per track. Visitors'
embeddings are never written to disk: a record holds one snapshot (data/snapshots/unknown/<day>/), the times and
the closest enrolled person with their score.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from app import config


def qualifies(track, min_frames: int, max_score: float, live_needed: int) -> bool:
    """Is this undecided track an unknown visitor? (See the module docstring.) `track`: app.tracker.TrackState."""
    samples = track.samples
    return (track.decided is None and len(samples) >= min_frames and track.live_frames >= live_needed
            and all(s.match.name is None and s.match.score < max_score for s in samples))


def track_embedding(samples) -> np.ndarray | None:
    """The kept frames' embeddings averaged into one unit vector (None if none was kept)."""
    rows = [s.embedding for s in samples if s.embedding is not None]
    if not rows:
        return None
    mean = np.mean([r / np.linalg.norm(r) for r in rows], axis=0)
    norm = np.linalg.norm(mean)
    return mean / norm if norm > 0 else None


def closest(samples) -> tuple[str | None, float]:
    """The enrolled person the track came closest to, and that best score (None, 0.0 with an empty gallery)."""
    best = max(samples, key=lambda s: s.match.score)
    return best.match.best_name, float(best.match.score)


@dataclass(eq=False)
class Visit:
    """One unknown visitor record as the worker holds it. `id` is None until the database has it."""
    first_seen: datetime
    last_seen: datetime
    score: float  # the closest enrolled person's best score
    closest: str | None  # their employee code
    liveness: float | None
    face_px: int | None
    snapshot: str | None  # relative to data/
    tracks: int = 1
    id: int | None = None
    deleted: bool = False  # withdrawn (recognised after all): the record and snapshot go
    queued: bool = False  # waiting in the worker's list of database writes
    retry: bool = False  # an insert failed once (maybe after the database committed it): look for it first


class StrangerMemory:
    """The embeddings of the strangers recorded recently by one camera, in memory only (see the module docstring)."""

    def __init__(self, seconds: float | None = None, same_person: float | None = None):
        self.seconds = config.UNKNOWN_MERGE_SECONDS if seconds is None else seconds
        self.same_person = config.UNKNOWN_SAME_PERSON if same_person is None else same_person
        self._seen: list[tuple[datetime, np.ndarray, Visit]] = []

    def __len__(self) -> int:
        return len(self._seen)

    def _prune(self, now: datetime) -> None:
        since = now - timedelta(seconds=self.seconds)
        self._seen = [s for s in self._seen if s[0] >= since and not s[2].deleted]

    def find(self, embedding: np.ndarray | None, now: datetime) -> Visit | None:
        """The visit of the stranger this face matches best, if one was seen within the window and matches well
        enough."""
        self._prune(now)
        if embedding is None or not self._seen:
            return None
        sims = [float(e @ embedding) for _, e, _ in self._seen]
        i = int(np.argmax(sims))
        return self._seen[i][2] if sims[i] >= self.same_person else None

    def remember(self, embedding: np.ndarray | None, now: datetime, visit: Visit) -> None:
        if embedding is not None:
            self._seen.append((now, embedding, visit))

    def forget(self, visit: Visit) -> None:
        self._seen = [s for s in self._seen if s[2] is not visit]
