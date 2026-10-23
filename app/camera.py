"""Opening video sources: a webcam index, a video file path or an RTSP URL."""

import re
import sys
import threading
import time
from collections import deque
from datetime import date, datetime

import cv2
import numpy as np

from app import config


def parse_source(value: str | int) -> str | int:
    """'0' -> 0 (webcam index); anything else stays a path or URL."""
    if isinstance(value, int):
        return value
    return int(value) if value.strip().isdigit() else value


def is_webcam(source: str | int) -> bool:
    return isinstance(source, int)


def is_live(source: str | int) -> bool:
    """Webcams and network streams are live; anything else is a video file read frame by frame."""
    return is_webcam(source) or bool(re.match(r"^[a-z][a-z0-9+.-]*://", str(source), re.I))


class LiveCapture:
    """Opens and reads a live source on a background thread, keeping only the newest frame.

    A webcam that another app holds can block in open() or read() for a long
    time; this keeps the worker's main loop (status, Stop, gallery reload)
    running regardless. It never gives up: a failed open, or `max_failures`
    failed reads in a row (a USB webcam unplugged or reset), releases the
    camera and opens it again after a pause from `backoff` (WEBCAM_RECONNECT_BACKOFF;
    back to the first pause once frames arrive again). `opener` is injectable for tests.
    """

    def __init__(self, source: str | int, opener=None, max_failures: int | None = None, backoff=None, log=None):
        self.source = source
        self._opener = opener or open_capture
        self._max_failures = config.WEBCAM_READ_FAILURES if max_failures is None else max_failures
        self.backoff = tuple(backoff or config.WEBCAM_RECONNECT_BACKOFF)
        self._log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._stop = threading.Event()
        self.opened = False  # opened at least once
        self.error: str | None = None  # never set (the worker's "give up" check): a live camera is always retried
        self.state = "connecting"  # connecting / live / reconnecting / stopped
        self.last_error: str | None = None  # why it last had to reopen: "could not open source", "lost the camera"
        self.reconnects = 0
        self.reconnect_times: deque[float] = deque(maxlen=1000)
        self.delays: list[float] = []  # pauses before reopening so far (for tests and the log)
        self.fps = 0.0  # frames delivered by the camera, before throttling
        self._frame_wall: float | None = None  # wall time of the newest frame
        self._live_since: float | None = None
        self._thread = threading.Thread(target=self._run, name=f"capture-{source}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        failures = 0  # attempts in a row that delivered no frame (for the backoff)
        while not self._stop.is_set():
            frames, problem = self._connection()
            if self._stop.is_set():
                break
            if frames:
                failures = 0
            delay = self.backoff[min(failures, len(self.backoff) - 1)]
            failures += 1
            with self._lock:
                self.state, self.last_error, self._live_since = "reconnecting", problem, None
                self.reconnects += 1
                self.reconnect_times.append(time.time())
                self.delays.append(delay)
            self._log(f"{datetime.now():%H:%M:%S}  camera {mask_source(self.source)}: {problem}; "
                      f"opening it again in {delay:g} s (attempt {failures})")
            if self._stop.wait(delay):
                break
        self.state = "stopped"

    def _connection(self) -> tuple[int, str | None]:
        """Open the source and read until it fails; returns (frames read, why it ended)."""
        try:
            cap = self._opener(self.source)
        except Exception as e:  # noqa: BLE001  RuntimeError from open_capture, or a driver error: try again later
            return 0, "could not open source" + ("" if isinstance(e, RuntimeError) else f" ({type(e).__name__})")
        self.opened = True
        frames, failures, count, window = 0, 0, 0, time.monotonic()
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= self._max_failures:
                        return frames, "lost the camera"
                    time.sleep(0.02)
                    continue
                failures = 0
                frames += 1
                with self._lock:
                    self._frame, self._seq, self._frame_wall = frame, self._seq + 1, time.time()
                    self._live_since = self._live_since or self._frame_wall
                    self.state = "live"
                count += 1
                if (elapsed := time.monotonic() - window) >= 2:
                    self.fps, count, window = count / elapsed, 0, time.monotonic()
            return frames, None
        finally:
            cap.release()

    def latest(self, after: int = 0) -> tuple[int, np.ndarray] | None:
        """(sequence number, frame) if a frame newer than `after` arrived, else None."""
        with self._lock:
            if self._seq > after and self._frame is not None:
                return self._seq, self._frame
        return None

    def health(self) -> dict:
        """Same fields as FrameSource.health()."""
        with self._lock:
            frame, frame_wall, live_since = self._frame, self._frame_wall, self._live_since
            state, last_error, reconnects = self.state, self.last_error, self.reconnects
            times = list(self.reconnect_times)
        today = date.today()
        return {
            "kind": "webcam" if is_webcam(self.source) else "stream",
            "state": state,
            "width": frame.shape[1] if frame is not None else None,
            "height": frame.shape[0] if frame is not None else None,
            "input_fps": round(self.fps, 1),
            "last_frame_at": frame_wall,
            "reconnects": reconnects,
            "reconnects_today": sum(date.fromtimestamp(t) == today for t in times),
            "last_error": last_error,
            "live_since": live_since,
        }

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the reader to finish; a read stuck in the driver is left to die with the process."""
        self._stop.set()
        self._thread.join(timeout)


def mask_source(source: str | int) -> str:
    """Hide the password in rtsp://user:password@host URLs before showing or logging them."""
    return re.sub(r"(://[^:/@\s]+:)[^@\s]*@", r"\1***@", str(source))


def open_capture(source: str | int) -> cv2.VideoCapture:
    """Open a source; raise RuntimeError if it can't be opened."""
    if is_webcam(source) and sys.platform == "win32":
        cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)  # DirectShow opens webcams much faster on Windows
    else:
        cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"could not open video source {source!r}")
    return cap
