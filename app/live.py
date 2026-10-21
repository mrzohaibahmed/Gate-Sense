"""Live view: each worker's latest annotated frame and its recent decisions.

The worker writes config.LIVE_DIR/<camera>.jpg (the frame it already processed
and drew boxes on, no extra detection) and <camera>.json (fps, frame time and
the last LIVE_DECISIONS decisions), at most every LIVE_FRAME_SECONDS, both
atomically. The dashboard reads them; a frame older than LIVE_STALE_SECONDS
means the camera isn't sending frames. The files are deleted when the worker stops.
"""

import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from app import config
from app.fileutil import atomic_write_bytes

NOT_SENDING = "camera not sending frames"
WAITING = "waiting for the first frame"


def frame_path(camera: str, live_dir: Path | None = None) -> Path:
    return Path(live_dir or config.LIVE_DIR) / f"{camera}.jpg"


def info_path(camera: str, live_dir: Path | None = None) -> Path:
    return Path(live_dir or config.LIVE_DIR) / f"{camera}.json"


def clear(camera: str, live_dir: Path | None = None) -> None:
    for path in (frame_path(camera, live_dir), info_path(camera, live_dir)):
        path.unlink(missing_ok=True)


class LiveWriter:
    """Used inside a worker. `clock` is injectable for tests."""

    def __init__(self, camera: str, live_dir: Path | None = None, interval: float | None = None,
                 keep: int | None = None, clock=time.monotonic):
        self.camera = camera
        self.live_dir = Path(live_dir or config.LIVE_DIR)
        self.interval = config.LIVE_FRAME_SECONDS if interval is None else interval
        self.decisions: deque[dict] = deque(maxlen=keep or config.LIVE_DECISIONS)
        self.clock = clock
        self._last = None
        self.started: float | None = None  # wall time the camera loop began

    def start(self) -> None:
        """Mark the start of the camera loop (after model loading); the frame wait is measured from here."""
        self.started = time.time()
        self.write_info(None, None)

    def decision(self, kind: str, track: int | None = None, name: str | None = None, score: float | None = None,
                 second: float | None = None, detail: str = "") -> None:
        """kind: match, unknown, skip, event, cool-down, not saved, closed."""
        self.decisions.append({
            "time": datetime.now().strftime("%H:%M:%S"), "kind": kind, "track": track, "name": name,
            "score": None if score is None else round(float(score), 3),
            "second": None if second is None else round(float(second), 3), "detail": detail,
        })

    def frame(self, img: np.ndarray, fps: float | None = None, warning: str | None = None) -> bool:
        """Write the frame and info if LIVE_FRAME_SECONDS have passed; True if written."""
        now = self.clock()
        if self._last is not None and now - self._last < self.interval:
            return False
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, config.LIVE_JPEG_QUALITY])
        if not ok:
            return False
        self._last = now
        atomic_write_bytes(frame_path(self.camera, self.live_dir), buf.tobytes())
        self.write_info(fps, warning, frame_time=time.time())
        return True

    def write_info(self, fps: float | None, warning: str | None, frame_time: float | None = None) -> None:
        """Also called without a frame (camera silent) so decisions and warnings stay current."""
        info = _read_json(info_path(self.camera, self.live_dir)) or {}
        atomic_write_bytes(info_path(self.camera, self.live_dir), json.dumps({
            "camera": self.camera, "updated": time.time(), "started": self.started,
            "frame_time": frame_time if frame_time is not None else info.get("frame_time"),
            "fps": None if fps is None else round(fps, 1), "warning": warning,
            "decisions": list(self.decisions),
        }).encode("utf-8"))

    def clear(self) -> None:
        clear(self.camera, self.live_dir)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class LiveView:
    image: bytes | None
    frame_age: float | None  # seconds since the frame was written; None = no frame yet
    fps: float | None
    warning: str | None  # from the worker, e.g. black frames
    decisions: list[dict]
    waiting_for: float | None = None  # no frame yet: seconds since the camera loop began (None = still loading)


def read_view(camera: str, live_dir: Path | None = None) -> LiveView:
    info = _read_json(info_path(camera, live_dir)) or {}
    try:
        image = frame_path(camera, live_dir).read_bytes()
    except OSError:
        image = None
    frame_time = info.get("frame_time")
    age = None if image is None or frame_time is None else max(0.0, time.time() - frame_time)
    started = info.get("started")
    waiting = max(0.0, time.time() - started) if age is None and started else None
    return LiveView(image, age, info.get("fps"), info.get("warning"), info.get("decisions", []), waiting)


def frame_message(frame_age: float | None, waiting_for: float | None) -> str | None:
    """What to show instead of (or over) the frame; None when the frame is fresh.

    waiting_for: with no frame yet, seconds since the worker's camera loop began
    (None = the worker is still loading its models).
    """
    if frame_age is None:
        if waiting_for is None or waiting_for <= config.LIVE_STALE_SECONDS:
            return WAITING
        return NOT_SENDING
    return NOT_SENDING if frame_age > config.LIVE_STALE_SECONDS else None


class CameraHealth:
    """Tracks whether a live camera delivers usable frames; returns a warning to log and show."""

    def __init__(self, clock=time.monotonic, hint: str | None = None):
        self.clock = clock
        self.hint = hint  # replaces the default "another app" hint (e.g. for IP cameras)
        self.started = self.last_frame = clock()
        self.black_since = None
        self.got_frame = False

    def update(self, frame: np.ndarray | None = None) -> str | None:
        """Call every loop iteration with the new frame, or None when there was none."""
        now = self.clock()
        if frame is not None:
            self.got_frame = True
            self.last_frame = now
            if float(frame.mean()) < config.BLACK_FRAME_LEVEL:
                self.black_since = self.black_since or now
            else:
                self.black_since = None
        # Fixed texts (no running seconds), so a warning is logged once when it starts and once when it clears.
        limit = config.CAMERA_WARN_SECONDS
        if now - self.last_frame >= limit:
            what = "no frames from the camera" if self.got_frame else "the camera hasn't sent a frame"
            return f"{what} for over {limit:.0f} s; " + (
                self.hint or "is another app (e.g. the Enrollment page) using it?")
        if self.black_since is not None and now - self.black_since >= limit:
            return f"the camera has sent only black frames for over {limit:.0f} s; " + (
                self.hint or "is another app (e.g. the Enrollment page) using it, or is the lens covered?")
        return None
