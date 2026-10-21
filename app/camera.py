"""Opening video sources: a webcam index, a video file path or an RTSP URL."""

import re
import sys
import threading
import time

import cv2
import numpy as np


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
    running regardless. `opener` is injectable for tests.
    """

    def __init__(self, source: str | int, opener=None, max_failures: int = 50):
        self.source = source
        self._opener = opener or open_capture
        self._max_failures = max_failures
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._stop = threading.Event()
        self.opened = False
        self.error: str | None = None  # "could not open source" or "lost the camera"
        self.fps = 0.0  # frames delivered by the camera, before throttling
        self._frame_wall: float | None = None  # wall time of the newest frame
        self._live_since: float | None = None
        self._thread = threading.Thread(target=self._run, name=f"capture-{source}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            cap = self._opener(self.source)
        except RuntimeError:
            self.error = "could not open source"
            return
        self.opened = True
        failures, count, window = 0, 0, time.monotonic()
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= self._max_failures:
                        self.error = "lost the camera"
                        return
                    time.sleep(0.02)
                    continue
                failures = 0
                with self._lock:
                    self._frame, self._seq, self._frame_wall = frame, self._seq + 1, time.time()
                    self._live_since = self._live_since or self._frame_wall
                count += 1
                if (elapsed := time.monotonic() - window) >= 2:
                    self.fps, count, window = count / elapsed, 0, time.monotonic()
        finally:
            cap.release()

    def latest(self, after: int = 0) -> tuple[int, np.ndarray] | None:
        """(sequence number, frame) if a frame newer than `after` arrived, else None."""
        with self._lock:
            if self._seq > after and self._frame is not None:
                return self._seq, self._frame
        return None

    def health(self) -> dict:
        """Same fields as FrameSource.health(); a webcam doesn't reconnect."""
        with self._lock:
            frame, frame_wall, live_since = self._frame, self._frame_wall, self._live_since
        state = "stopped" if self.error else "live" if frame is not None else "connecting"
        return {
            "kind": "webcam" if is_webcam(self.source) else "stream",
            "state": state,
            "width": frame.shape[1] if frame is not None else None,
            "height": frame.shape[0] if frame is not None else None,
            "input_fps": round(self.fps, 1),
            "last_frame_at": frame_wall,
            "reconnects": 0,
            "reconnects_today": 0,
            "last_error": self.error,
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
