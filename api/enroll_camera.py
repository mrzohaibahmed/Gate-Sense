"""Enrollment from an IP camera: the frames behind the wizard's "Which camera?".

A session holds one camera for one user's wizard. Its frames come from:
- the camera's running worker, when there is one: the worker shares its raw full-resolution frames
  (data/live/<camera>.full.jpg, while the session keeps <camera>.enroll fresh), so attendance goes on and the
  camera isn't opened twice;
- otherwise a connection the API opens itself, on the camera's main stream (a Hikvision camera set to the sub
  stream is read at 101). While it is open the camera's worker can't start (WorkerManager.camera_holds), like the
  webcam hold. If a shared worker stops, the session switches to its own connection.

The wizard checks and captures the centred enrollment window (ENROLL_IP_WINDOW of the frame) at full resolution,
so the guide box and the step checks are the webcam's, and a busy gate scene still gives a one-face photo. Sessions
are renewed like the webcam claim and lapse after ENROLL_CAMERA_CLAIM_SECONDS without a renewal. The camera's
source (with its password) stays in this process: the browser only gets frames.
"""

import secrets
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from api.workers import CameraBusy, WorkerManager
from app import camera_settings, config, live, sources
from app.camera import mask_source
from app.frame_source import open_live
from app.workers import WorkerError

STALE_SHARE_SECONDS = 5.0  # a shared worker that sent nothing for this long (and has stopped) is replaced


def window(frame: np.ndarray) -> np.ndarray:
    """The centred enrollment window of a full-resolution frame (a view, not a copy)."""
    h, w = frame.shape[:2]
    ww, wh = max(1, round(w * config.ENROLL_IP_WINDOW)), max(1, round(h * config.ENROLL_IP_WINDOW))
    x, y = (w - ww) // 2, (h - wh) // 2
    return frame[y:y + wh, x:x + ww]


def main_stream(entry: dict):
    """The camera's source on its main stream: a Hikvision camera set to the sub stream (102) is read at 101."""
    if entry.get("type") == "hikvision" and entry.get("stream") != "main":
        return camera_settings.build({**entry, "stream": "main"}, [])["source"]
    return entry["source"]


def default_opener(source, name: str):
    """A newest-frame reader for a live source (FrameSource for RTSP, LiveCapture otherwise)."""
    reader = open_live(source, log=lambda message: print(f"[enrollment {name}] {mask_source(str(message))}",
                                                         flush=True))
    if reader is None:
        raise WorkerError(f"{name} isn't a live camera")
    return reader


class OwnFeed:
    """The camera opened by the API for the wizard."""

    mode = "camera"

    def __init__(self, name: str, source, opener):
        self.name = name
        self.reader = opener(source, name)  # latest(after) -> (seq, frame) | None, health() -> dict, stop()

    def latest(self) -> tuple[int, np.ndarray] | None:
        return self.reader.latest(0)

    def error(self) -> str | None:
        health = self.reader.health() or {}
        err = health.get("last_error") or getattr(self.reader, "error", None)
        return mask_source(str(err)) if err else None

    def keep_alive(self) -> None:
        pass

    def close(self) -> None:
        self.reader.stop()


class WorkerFeed:
    """Frames shared by the camera's running worker."""

    mode = "worker"

    def __init__(self, name: str, live_dir):
        self.name, self.live_dir = name, live_dir
        self._lock = threading.Lock()
        self._key, self._seq, self._frame = None, 0, None
        self.last_new = time.monotonic()
        live.mark_enrolling(name, live_dir)

    def latest(self) -> tuple[int, np.ndarray] | None:
        with self._lock:
            path = live.full_path(self.name, self.live_dir)
            try:
                st = path.stat()
            except OSError:
                st = None
            if st is not None and (st.st_mtime_ns, st.st_size) != self._key:
                img = cv2.imread(str(path))
                if img is not None:
                    self._key, self._seq, self._frame = (st.st_mtime_ns, st.st_size), self._seq + 1, img
                    self.last_new = time.monotonic()
            return (self._seq, self._frame) if self._frame is not None else None

    def error(self) -> str | None:
        return None

    def keep_alive(self) -> None:
        live.mark_enrolling(self.name, self.live_dir)

    def close(self) -> None:
        live.stop_enrolling(self.name, self.live_dir)


@dataclass
class EnrollSession:
    id: str
    user: str
    camera: str
    camera_id: int
    feed: OwnFeed | WorkerFeed
    seen: float
    closed: bool = False
    _count: int = 0
    _key: tuple | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def latest(self) -> tuple[int, np.ndarray] | None:
        """(sequence number, full-resolution frame), the number growing across a switch of feed."""
        with self._lock:
            feed = self.feed
        got = feed.latest()
        if got is None:
            return None
        with self._lock:
            key = (id(feed), got[0])
            if key != self._key:
                self._key, self._count = key, self._count + 1
            return self._count, got[1]


class EnrollCameras:
    """The wizard's IP camera sessions. `opener(source, name)` is injectable for tests."""

    def __init__(self, workers: WorkerManager, opener=None):
        self.workers = workers
        self.opener = opener or default_opener
        self._sessions: dict[str, EnrollSession] = {}
        self._thread: threading.Thread | None = None
        workers.camera_holds = self.holders  # its start() refuses a camera the wizard has open itself

    # --- Sessions -----------------------------------------------------------------------------------------

    def open(self, user: str, camera_id: int, entry: dict, session_id: str | None = None) -> tuple[EnrollSession, bool]:
        """Open (or renew) a session on this camera; (session, new). CameraBusy if another user's wizard has it;
        WorkerError if it can't be opened (a missing password variable)."""
        name = entry["name"]
        with self.workers.lock:
            self._expire()
            mine = self._sessions.get(session_id) if session_id else None
            if mine and mine.user == user and mine.camera == name:
                mine.seen = time.monotonic()
                return mine, False
            for other in [s for s in self._sessions.values() if s.camera == name]:
                if other.user != user:
                    raise CameraBusy(f"the {name} camera is in use by the enrollment wizard ({other.user}); "
                                     "wait until they finish")
                self._close(other)  # the same user again (a reload, a second tab): the newest wins
            feed = self._feed(name, entry)
            session = EnrollSession(secrets.token_urlsafe(16), user, name, camera_id, feed, time.monotonic())
            self._sessions[session.id] = session
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._tick, name="enroll-cameras", daemon=True)
                self._thread.start()
            return session, True

    def _feed(self, name: str, entry: dict) -> OwnFeed | WorkerFeed:
        if self.workers.status(name).state != "stopped":
            return WorkerFeed(name, self.workers.live_dir)
        source = main_stream(entry)
        if missing := sources.missing_variables(source):
            raise WorkerError(str(sources.MissingVariables(missing)))
        return OwnFeed(name, sources.expand(source), self.opener)

    def get(self, user: str, session_id: str) -> EnrollSession | None:
        with self.workers.lock:
            self._expire()
            s = self._sessions.get(session_id)
            return s if s is not None and s.user == user else None

    def release(self, user: str, session_id: str) -> bool:
        with self.workers.lock:
            s = self._sessions.get(session_id)
            if s is None or s.user != user:
                return False
            self._close(s)
            return True

    def holders(self, name: str) -> list[str]:
        """Users whose wizard has this camera open on its own connection (its worker can't start meanwhile)."""
        with self.workers.lock:
            self._expire()
            return sorted({s.user for s in self._sessions.values() if s.camera == name and s.feed.mode == "camera"})

    def close_all(self) -> None:
        with self.workers.lock:
            for s in list(self._sessions.values()):
                self._close(s)

    def _close(self, s: EnrollSession) -> None:
        self._sessions.pop(s.id, None)
        s.closed = True
        s.feed.close()

    def _expire(self) -> None:
        now = time.monotonic()
        for s in list(self._sessions.values()):
            if now - s.seen > config.ENROLL_CAMERA_CLAIM_SECONDS:
                self._close(s)

    # --- Housekeeping ------------------------------------------------------------------------------------

    def _tick(self) -> None:
        """Every second while there are sessions: lapse unrenewed ones, keep shared workers sharing, and replace a
        shared worker that stopped with the session's own connection."""
        while True:
            time.sleep(1.0)
            with self.workers.lock:
                self._expire()
                if not self._sessions:
                    return
                for s in list(self._sessions.values()):
                    try:
                        self.check_feed(s)
                    except Exception as e:  # noqa: BLE001  never let one camera stop the others' housekeeping
                        print(f"[enrollment {s.camera}] {mask_source(str(e))}", flush=True)

    def check_feed(self, s: EnrollSession) -> None:
        s.feed.keep_alive()
        if s.feed.mode != "worker" or time.monotonic() - s.feed.last_new < STALE_SHARE_SECONDS:
            return
        if self.workers.status(s.camera).state != "stopped":
            return  # still running (perhaps just quiet): keep waiting for its frames
        entry = next((e for e in camera_settings.entries() if e["name"] == s.camera), None)
        if entry is None:
            return
        new = self._feed(s.camera, entry)
        with s._lock:
            old, s.feed = s.feed, new
        old.close()
        print(f"[enrollment {s.camera}] the worker stopped: reading the camera directly", flush=True)
