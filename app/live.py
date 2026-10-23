"""Live view: each worker's latest annotated frame and its recent decisions.

The worker writes config.LIVE_DIR/<camera>.jpg (the frame it already processed
and drew boxes on, no extra detection) and <camera>.json (fps, frame time and
the last LIVE_DECISIONS decisions), at most every LIVE_FRAME_SECONDS, both
atomically. The dashboard reads them; a frame older than LIVE_STALE_SECONDS
means the camera isn't sending frames. The files are deleted when the worker stops.

While the API streams a camera it keeps <camera>.watch fresh (mark_watched); the
worker then writes the frame up to STREAM_FPS times a second (LiveWriter.watched),
so the stream gets every processed frame, encoded once for all viewers.

While the enrollment wizard uses a running camera it keeps <camera>.enroll fresh
(mark_enrolling); the worker then shares its raw full-resolution frame as
<camera>.full.jpg up to ENROLL_FEED_FPS times a second (LiveWriter.full_frame),
so the wizard never opens a second connection to the camera.
"""

import json
import threading
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


def enroll_path(camera: str, live_dir: Path | None = None) -> Path:
    """Kept fresh by the API while the enrollment wizard uses this camera's running worker."""
    return Path(live_dir or config.LIVE_DIR) / f"{camera}.enroll"


def full_path(camera: str, live_dir: Path | None = None) -> Path:
    """The newest raw full-resolution frame, written by the worker for the enrollment wizard."""
    return Path(live_dir or config.LIVE_DIR) / f"{camera}.full.jpg"


def mark_enrolling(camera: str, live_dir: Path | None = None) -> None:
    path = enroll_path(camera, live_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def stop_enrolling(camera: str, live_dir: Path | None = None) -> None:
    enroll_path(camera, live_dir).unlink(missing_ok=True)
    full_path(camera, live_dir).unlink(missing_ok=True)


def is_enrolling(camera: str, live_dir: Path | None = None) -> bool:
    try:
        return time.time() - enroll_path(camera, live_dir).stat().st_mtime <= config.ENROLL_FEED_WATCH_SECONDS
    except OSError:
        return False


def watch_path(camera: str, live_dir: Path | None = None) -> Path:
    return Path(live_dir or config.LIVE_DIR) / f"{camera}.watch"


def mark_watched(camera: str, live_dir: Path | None = None) -> None:
    """Called by the API about once a second while at least one client streams this camera."""
    path = watch_path(camera, live_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def unwatch(camera: str, live_dir: Path | None = None) -> None:
    watch_path(camera, live_dir).unlink(missing_ok=True)


def is_watched(camera: str, live_dir: Path | None = None) -> bool:
    try:
        return time.time() - watch_path(camera, live_dir).stat().st_mtime <= config.STREAM_WATCH_SECONDS
    except OSError:
        return False


def clear(camera: str, live_dir: Path | None = None) -> None:
    for path in (frame_path(camera, live_dir), info_path(camera, live_dir), full_path(camera, live_dir)):
        path.unlink(missing_ok=True)


class LiveWriter:
    """Used inside a worker. `clock` is injectable for tests."""

    def __init__(self, camera: str, live_dir: Path | None = None, interval: float | None = None,
                 keep: int | None = None, clock=time.monotonic, background: bool = True):
        self.camera = camera
        self.live_dir = Path(live_dir or config.LIVE_DIR)
        self.interval = config.LIVE_FRAME_SECONDS if interval is None else interval
        self.decisions: deque[dict] = deque(maxlen=keep or config.LIVE_DECISIONS)
        self.clock = clock
        self._last = None
        self.started: float | None = None  # wall time the camera loop began
        self._watched, self._watch_checked = False, None
        # Full frames for the enrollment wizard: encoded on a thread so recognition never waits for it.
        self.background = background
        self._enrolling, self._enroll_checked, self._last_full = False, None, None
        self._full_pending: np.ndarray | None = None
        self._full_ready = threading.Event()
        self._full_thread: threading.Thread | None = None

    def start(self) -> None:
        """Mark the start of the camera loop (after model loading); the frame wait is measured from here."""
        self.started = time.time()
        self.write_info(None, None)

    def decision(self, kind: str, track: int | None = None, name: str | None = None, score: float | None = None,
                 second: float | None = None, detail: str = "", face_px: int | None = None) -> None:
        """kind: match, unknown, skip, event, cool-down, not saved, closed. face_px: face width in camera pixels."""
        self.decisions.append({
            "time": datetime.now().strftime("%H:%M:%S"), "kind": kind, "track": track, "name": name,
            "score": None if score is None else round(float(score), 3),
            "second": None if second is None else round(float(second), 3),
            "face_px": None if face_px is None else int(face_px), "detail": detail,
        })

    def watched(self) -> bool:
        """Is the API streaming this camera? (The .watch file is checked at most twice a second.)"""
        now = self.clock()
        if self._watch_checked is None or now - self._watch_checked >= 0.5:
            self._watch_checked = now
            self._watched = is_watched(self.camera, self.live_dir)
        return self._watched

    def enrolling(self) -> bool:
        """Is the enrollment wizard using this camera? (The .enroll file is checked at most twice a second; the
        shared frame is deleted when it goes stale.)"""
        now = self.clock()
        if self._enroll_checked is None or now - self._enroll_checked >= 0.5:
            self._enroll_checked = now
            was, self._enrolling = self._enrolling, is_enrolling(self.camera, self.live_dir)
            if was and not self._enrolling:
                full_path(self.camera, self.live_dir).unlink(missing_ok=True)
        return self._enrolling

    def full_frame(self, img: np.ndarray) -> bool:
        """While the enrollment wizard uses this camera, share the raw full-resolution frame (at most ENROLL_FEED_FPS
        a second) so it needn't open a second connection; True if one was handed over."""
        if not self.enrolling():
            return False
        now = self.clock()
        if self._last_full is not None and now - self._last_full < 1.0 / config.ENROLL_FEED_FPS:
            return False
        self._last_full = now
        if not self.background:
            self._write_full(img)
            return True
        self._full_pending = img  # the capture never draws on its frames, so holding a reference is safe
        if self._full_thread is None:
            self._full_thread = threading.Thread(target=self._full_writer, name="enroll-feed", daemon=True)
            self._full_thread.start()
        self._full_ready.set()
        return True

    def _full_writer(self) -> None:
        while True:
            self._full_ready.wait()
            self._full_ready.clear()
            img, self._full_pending = self._full_pending, None
            if img is not None:
                self._write_full(img)

    def _write_full(self, img: np.ndarray) -> None:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, config.ENROLL_CAPTURE_QUALITY])
        if ok:
            atomic_write_bytes(full_path(self.camera, self.live_dir), buf.tobytes())

    def frame(self, img: np.ndarray, fps: float | None = None, warning: str | None = None) -> bool:
        """Write the frame and info if LIVE_FRAME_SECONDS have passed (1 / STREAM_FPS while watched); True if
        written."""
        now = self.clock()
        interval = min(self.interval, 1.0 / config.STREAM_FPS) if self.watched() else self.interval
        if self._last is not None and now - self._last < interval:
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


def read_info(camera: str, live_dir: Path | None = None) -> dict:
    """The worker's live info (fps, frame_time, warning, decisions) without the frame; {} if none."""
    return _read_json(info_path(camera, live_dir)) or {}


def read_view(camera: str, live_dir: Path | None = None) -> LiveView:
    info = read_info(camera, live_dir)
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
