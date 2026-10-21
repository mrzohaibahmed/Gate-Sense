"""Frame sources for the camera worker.

- rtsp:// (IP cameras such as Hikvision): FrameSource. Opens with FFmpeg over TCP with
  5 s open/read timeouts, reads on a background thread keeping only the newest frame,
  and reconnects forever with backoff (1, 2, 4, 8, 16, 30 s) when opening fails,
  read() fails, or no new frame arrives for RTSP_STALE_SECONDS. It never gives up, so a
  dropped camera never stops the worker.
- Webcam index (and other URLs): app.camera.LiveCapture, unchanged.
- Video file: read in order by the worker (app.camera.open_capture), unchanged.

Every message and error text is passed through mask_source, so camera passwords
never reach the log or the dashboard.
"""

import os
import threading
import time
from collections import deque
from datetime import date, datetime

import cv2
import numpy as np

from app import config
from app.camera import LiveCapture, is_live, is_webcam, mask_source


def kind(source) -> str:
    """'rtsp', 'webcam', 'stream' (other URLs) or 'file'."""
    if is_webcam(source):
        return "webcam"
    if str(source).lower().startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    return "stream" if is_live(source) else "file"


def open_live(source, log=print):
    """The worker's live reader: FrameSource for RTSP, the existing LiveCapture otherwise (files: None)."""
    k = kind(source)
    if k == "rtsp":
        return FrameSource(source, log=log)
    if k in ("webcam", "stream"):
        return LiveCapture(source)
    return None


def fourcc_text(value: float) -> str:
    """CAP_PROP_FOURCC as text, e.g. 'h264', 'hevc', 'MJPG'; 'unknown' if the backend doesn't say."""
    code = int(value or 0)
    text = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")
    return text if code > 0 and text and text.isprintable() else "unknown"


def _prop(cap, prop: int) -> float:
    try:
        return float(cap.get(prop))
    except Exception:  # noqa: BLE001  fake or limited capture objects
        return 0.0


def open_rtsp(url: str) -> cv2.VideoCapture:
    """VideoCapture for an RTSP URL: FFmpeg backend, TCP, 5 s open and read timeouts."""
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", config.RTSP_FFMPEG_OPTIONS)  # read at each open
    params = []
    for name, value in (("CAP_PROP_OPEN_TIMEOUT_MSEC", config.RTSP_OPEN_TIMEOUT_MS),
                        ("CAP_PROP_READ_TIMEOUT_MSEC", config.RTSP_READ_TIMEOUT_MS)):
        if hasattr(cv2, name):  # older OpenCV builds lack these
            params += [getattr(cv2, name), value]
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params) if params else cv2.VideoCapture(url, cv2.CAP_FFMPEG)


class _Connection:
    """One connection attempt: opens and reads on its own daemon thread until abandoned or failing.

    A read stuck in the driver can't be interrupted, so a stale connection is abandoned rather than
    joined; its thread releases the capture when (if) the read returns.
    """

    def __init__(self, source, opener, store, clock):
        self.source, self._opener, self._store, self._clock = source, opener, store, clock
        self.started = clock()
        self.last_frame: float | None = None
        self.frames = 0
        self.error: str | None = None
        self.size: tuple[int, int] | None = None
        self.codec: str | None = None  # from CAP_PROP_FOURCC
        self.reported_fps: float | None = None  # what the camera/stream says (CAP_PROP_FPS)
        self._abandoned = threading.Event()
        threading.Thread(target=self._run, name="rtsp-reader", daemon=True).start()

    def _run(self) -> None:
        try:
            cap = self._opener(self.source)
        except Exception as e:  # noqa: BLE001  any open problem just means "try again later"
            self.error = mask_source(f"open failed: {e}")
            return
        try:
            if cap is None or not cap.isOpened():
                self.error = "could not open the stream (wrong address, password or path, or camera offline)"
                return
            self.codec = fourcc_text(_prop(cap, cv2.CAP_PROP_FOURCC))
            fps = _prop(cap, cv2.CAP_PROP_FPS)
            self.reported_fps = fps if fps > 0 else None  # webcams often report 0 or -1
            while not self._abandoned.is_set():
                t0 = time.perf_counter()
                ok, frame = cap.read()
                read_s = time.perf_counter() - t0
                if self._abandoned.is_set():
                    return
                if not ok or frame is None:
                    self.error = "stream dropped (read failed)"
                    return
                now = self._clock()
                self.last_frame = now
                self.frames += 1
                self.size = (frame.shape[1], frame.shape[0])
                self._store(frame, now, read_s)
        finally:
            if cap is not None:
                cap.release()

    def abandon(self) -> None:
        self._abandoned.set()


class FrameSource:
    """Newest-frame reader for an RTSP camera, with health and reconnect-with-backoff.

    opener, clock, wall and wait are injectable for tests (wait(seconds) sleeps and returns
    True when the source is being stopped).
    """

    def __init__(self, source, opener=None, log=print, stale_after: float | None = None,
                 connect_grace: float | None = None, backoff=None, clock=time.monotonic, wall=time.time,
                 wait=None, poll: float = 0.05):
        self.source = source
        self._opener = opener or open_rtsp
        self._log = log
        self.stale_after = config.RTSP_STALE_SECONDS if stale_after is None else stale_after
        self.connect_grace = config.RTSP_CONNECT_GRACE_SECONDS if connect_grace is None else connect_grace
        self.backoff = tuple(backoff or config.RTSP_RECONNECT_BACKOFF)
        self._clock, self._wall, self._poll = clock, wall, poll
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._frame_wall: float | None = None
        self._arrivals: deque[float] = deque(maxlen=60)
        self._read_times: deque[float] = deque(maxlen=100)
        self.codec: str | None = None
        self.reported_fps: float | None = None

        self.state = "connecting"  # connecting / live / reconnecting / stopped
        self.error = None  # never set: an RTSP source never gives up (LiveCapture-compatible attribute)
        self.opened = False
        self.size: tuple[int, int] | None = None
        self.reconnects = 0
        self.reconnect_times: deque[float] = deque(maxlen=1000)
        self.last_error: str | None = None
        self.live_since: float | None = None
        self.delays: list[float] = []  # backoff waits so far (for tests and the log)
        self._thread = threading.Thread(target=self._supervise, name="rtsp-supervisor", daemon=True)
        self._thread.start()

    # --- reader side ---------------------------------------------------------------

    def _store(self, frame: np.ndarray, now: float, read_s: float = 0.0) -> None:
        with self._lock:
            self._frame, self._seq, self._frame_wall = frame, self._seq + 1, self._wall()
            self._arrivals.append(now)
            self._read_times.append(read_s)

    @property
    def read_ms(self) -> float:
        """Average time inside cap.read() (receive + decode; for a live camera it includes waiting for the frame)."""
        with self._lock:
            times = list(self._read_times)
        return 1000 * sum(times) / len(times) if times else 0.0

    def _supervise(self) -> None:
        failures = 0
        label = mask_source(self.source)
        while not self._stop.is_set():
            conn = _Connection(self.source, self._opener, self._store, self._clock)
            reason = self._watch(conn)
            conn.abandon()
            if reason is None:  # stopped
                break
            was_live = conn.frames > 0
            if was_live:
                failures = 0  # a working connection resets the backoff
            delay = self.backoff[min(failures, len(self.backoff) - 1)]
            failures += 1
            self.reconnects += 1
            self.reconnect_times.append(self._wall())
            self.last_error = reason
            self.state = "reconnecting"
            self.live_since = None
            self.delays.append(delay)
            what = "disconnected" if was_live else "could not connect"
            self._log(f"{datetime.now():%H:%M:%S}  camera {what} ({label}): {reason}; "
                      f"reconnecting in {delay:g} s (attempt {failures})")
            if self._wait(delay):
                break
        self.state = "stopped"

    def _watch(self, conn: _Connection) -> str | None:
        """Wait until the connection fails or goes stale; None if the source was stopped."""
        announced = False
        while not self._stop.is_set():
            if conn.error:
                return conn.error
            now = self._clock()
            if conn.frames and not announced:
                announced = True
                self.opened = True
                self.state, self.live_since, self.size = "live", self._wall(), conn.size
                self.codec, self.reported_fps = conn.codec, conn.reported_fps
                w, h = conn.size
                self._log(f"{datetime.now():%H:%M:%S}  camera live ({mask_source(self.source)}): {w}x{h}"
                          + (f" after {self.reconnects} reconnect(s)" if self.reconnects else ""))
            if conn.last_frame is None:
                if now - conn.started > self.connect_grace:
                    return f"no frame within {self.connect_grace:g} s of connecting"
            elif now - conn.last_frame > self.stale_after:
                return f"no new frame for {self.stale_after:g} s"
            if conn.size:
                self.size = conn.size
            time.sleep(self._poll)
        return None

    # --- worker side (same interface as LiveCapture) -----------------------------------

    def latest(self, after: int = 0) -> tuple[int, np.ndarray] | None:
        """(sequence number, frame) if a frame newer than `after` arrived, else None. Never a backlog."""
        with self._lock:
            if self._seq > after and self._frame is not None:
                return self._seq, self._frame
        return None

    @property
    def fps(self) -> float:
        """Frames per second arriving from the camera (last ~2 s)."""
        with self._lock:
            arrivals = [t for t in self._arrivals if self._clock() - t <= 2.0]
        if len(arrivals) < 2:
            return 0.0
        return (len(arrivals) - 1) / max(arrivals[-1] - arrivals[0], 1e-6)

    def health(self) -> dict:
        with self._lock:
            frame_wall = self._frame_wall
        today = date.today()
        return {
            "kind": "rtsp",
            "state": self.state,
            "codec": self.codec,
            "width": self.size[0] if self.size else None,
            "height": self.size[1] if self.size else None,
            "input_fps": round(self.fps, 1),
            "last_frame_at": frame_wall,
            "reconnects": self.reconnects,
            "reconnects_today": sum(date.fromtimestamp(t) == today for t in self.reconnect_times),
            "last_error": self.last_error,
            "live_since": self.live_since,
        }

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._thread.join(timeout)
        self.state = "stopped"
