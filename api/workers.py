"""Camera workers for the API.

Wraps app.workers.WorkerManager (the recognize.py processes and their
status/stop files under data/run/), and adds what the API needs:

- who holds a camera: this API or the command line (from the worker's
  --parent-pid in its status file);
- a clear CameraBusy error instead of opening a camera twice: a camera that
  any process already runs, a webcam another camera's worker uses, or the
  webcam or an IP camera while the enrollment wizard holds it (claim_webcam,
  camera_holds from api.enroll_camera). The worker
  itself also takes an OS lock per camera and webcam (app.workers.acquire_camera),
  so even two starts at the same moment can't both open it;
- fps, last frame time and last error per worker;
- open()/close() for the FastAPI lifespan: data/run/_api.json while running,
  and on shutdown every worker this API started is asked to stop, all at once.
"""

import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app import config, live
from app import workers as app_workers
from app.camera import is_webcam, parse_source
from app.fileutil import atomic_write_bytes
from app.workers import WorkerError

API = "_api"  # status file name for the running API process (data/run/_api.json)
CLEAN_STOPS = {"stop requested", "end of video", "window closed", "interrupted", "GateSense exited",
               "dashboard exited", "exit code 0"}  # "dashboard exited": written by older workers


class CameraBusy(WorkerError):
    """The camera (or its webcam) is already held by a worker or by the enrollment wizard."""


class WorkerInfo(BaseModel):
    camera: str
    state: str  # running, starting, not responding, stopped
    held_by: str | None = None  # api, command line, other; None when stopped
    pid: int | None = None
    started: datetime | None = None
    fps: float | None = None
    last_frame_at: datetime | None = None
    last_error: str | None = None
    warning: str | None = None
    detail: str = ""  # stop reason of the last run
    device: str | None = None  # where its models run: "NVIDIA GPU (CUDA)" or "CPU"; None while loading or stopped
    device_warning: str | None = None  # why it runs on the CPU although GATESENSE_DEVICE allowed the GPU


def api_pid(run_dir: Path | None = None) -> int | None:
    """PID of the running API process, if any (stale info files are ignored)."""
    try:
        data = json.loads(app_workers.status_path(API, run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data["pid"] if app_workers.pid_alive(data.get("pid")) else None


class WorkerManager:
    def __init__(self, manager: app_workers.WorkerManager | None = None):
        self._mgr = manager or app_workers.WorkerManager()
        self.started_at: float | None = None  # time.time() of open()
        # The enrollment wizard's hold on the webcam: claim id -> (user, time.monotonic() of the last renewal).
        # Only this process starts workers for the dashboard, so the claims live here; one lock with start().
        self._claims: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()
        # Users whose wizard has this camera open on its own connection (set by api.enroll_camera.EnrollCameras).
        self.camera_holds = lambda name: []

    @property
    def lock(self) -> threading.RLock:
        """Held while a camera is started or claimed for enrollment, so the two can't cross."""
        return self._lock

    @property
    def run_dir(self) -> Path:
        return self._mgr.run_dir

    @property
    def live_dir(self) -> Path:
        return self._mgr.live_dir

    @property
    def cameras(self) -> list[dict]:
        config.refresh_cameras()
        return self._mgr.cameras

    # --- Lifespan ----------------------------------------------------------------

    def open(self, url: str | None = None) -> None:
        """Mark this process as the running API; refuses if another API process is alive."""
        other = api_pid(self.run_dir)
        if other and other != os.getpid():
            raise RuntimeError(f"the GateSense API is already running (process {other})")
        self.started_at = time.time()
        atomic_write_bytes(app_workers.status_path(API, self.run_dir), json.dumps({
            "pid": os.getpid(), "url": url, "started": datetime.now().isoformat(timespec="seconds"),
        }).encode("utf-8"))

    def close(self) -> list[tuple[str, str]]:
        """Stop every worker this API started, in parallel (stop file, then kill after WORKER_STOP_TIMEOUT)."""
        names = self._mgr.managed()
        results = []
        if names:
            with ThreadPoolExecutor(max_workers=len(names)) as pool:
                results = list(zip(names, pool.map(self._mgr.stop, names)))
        app_workers.status_path(API, self.run_dir).unlink(missing_ok=True)
        return results

    # --- Status ------------------------------------------------------------------

    def holder(self, st: app_workers.WorkerStatus) -> str | None:
        if st.state == "stopped":
            return None
        if st.managed or st.parent_pid == os.getpid():
            return "api"
        if st.parent_pid is None:
            return "command line"
        return "other"

    def _describe(self, st: app_workers.WorkerStatus) -> str:
        return {
            "api": "started by this API",
            "command line": "started from the command line (scripts\\recognize.py)",
        }.get(self.holder(st), f"started by another GateSense process ({st.parent_pid})")

    def info(self, st: app_workers.WorkerStatus) -> WorkerInfo:
        health = st.health or {}
        frame_time = health.get("last_frame_at")
        if frame_time is None and st.state != "stopped":
            frame_time = live.read_info(st.camera, self._mgr.live_dir).get("frame_time")
        error = health.get("last_error")
        dev = (st.device or {}) if st.state != "stopped" else {}
        if st.state == "stopped" and st.detail and st.detail not in CLEAN_STOPS:
            error = error or st.detail
        return WorkerInfo(
            camera=st.camera, state=st.state, held_by=self.holder(st), pid=st.pid, started=st.started,
            fps=st.fps, last_frame_at=datetime.fromtimestamp(frame_time) if frame_time else None,
            last_error=error, warning=st.warning, detail=st.detail,
            device=dev.get("label"), device_warning=dev.get("warning"),
        )

    def status(self, name: str) -> WorkerInfo:
        config.refresh_cameras()
        return self.info(self._mgr.status(name))

    def statuses(self) -> list[WorkerInfo]:
        config.refresh_cameras()
        return [self.info(st) for st in self._mgr.statuses()]

    # --- Start / stop ------------------------------------------------------------

    def start(self, name: str) -> WorkerInfo:
        """Start the camera's worker; CameraBusy if any process already holds the camera or its webcam."""
        config.refresh_cameras()
        with self._lock:  # so a wizard can't claim the webcam between the check and the start
            st = self._mgr.status(name)  # WorkerError for an unknown camera
            if st.state != "stopped":
                raise CameraBusy(f"{name} is already running ({self._describe(st)}, process {st.pid}); "
                                 "it can't be opened twice")
            if users := self.camera_holds(name):
                raise CameraBusy(f"{name} is in use by the enrollment wizard ({', '.join(users)}); "
                                 "finish or close the enrollment first")
            source = self._mgr.camera(name)["source"]
            if is_webcam(parse_source(str(source))):
                if other := app_workers.webcam_conflict(name, source, self._mgr.cameras, self.run_dir):
                    raise CameraBusy(f"webcam {source} is already in use by the {other} worker "
                                     f"({self._describe(self._mgr.status(other))}); stop it first")
                if users := self.webcam_claimed_by():
                    raise CameraBusy(f"webcam {source} is in use by the enrollment wizard ({', '.join(users)}); "
                                     "finish or close the enrollment first")
            return self.info(self._mgr.start(name))

    # --- The enrollment wizard's webcam ---------------------------------------------

    def _expire_claims(self) -> None:
        now = time.monotonic()
        for claim_id, (_, seen) in list(self._claims.items()):
            if now - seen > config.ENROLL_CAMERA_CLAIM_SECONDS:
                del self._claims[claim_id]

    def claim_webcam(self, user: str, claim_id: str | None = None) -> str:
        """Hold the webcam for the enrollment wizard, or renew a claim; returns the claim id. A new claim is
        refused (CameraBusy) while a webcam camera worker runs; a renewal of a live claim never is. A claim that
        isn't renewed within ENROLL_CAMERA_CLAIM_SECONDS lapses (tab closed, browser crashed)."""
        config.refresh_cameras()
        with self._lock:
            self._expire_claims()
            if claim_id and claim_id in self._claims:
                self._claims[claim_id] = (user, time.monotonic())
                return claim_id
            if busy := self._mgr.webcam_users():
                raise CameraBusy(f"the webcam is in use by the {', '.join(busy)} camera worker; "
                                 "stop it on the Live cameras page first")
            claim_id = secrets.token_urlsafe(16)
            self._claims[claim_id] = (user, time.monotonic())
            return claim_id

    def release_webcam(self, claim_id: str) -> bool:
        """End a claim (the wizard closed); False if it had already lapsed."""
        with self._lock:
            return self._claims.pop(claim_id, None) is not None

    def webcam_claimed_by(self) -> list[str]:
        """Users whose enrollment wizard holds the webcam right now (empty = free)."""
        with self._lock:
            self._expire_claims()
            return sorted({user for user, _ in self._claims.values()})

    def log_tail(self, name: str, lines: int) -> list[str]:
        """The last `lines` lines of the camera's worker log (passwords masked)."""
        return self._mgr.log_tail(name, lines)

    def log_path(self, name: str) -> Path:
        return self._mgr.log_path(name)

    def stop(self, name: str) -> str:
        """Ask the worker to stop (whoever started it); returns the result text."""
        config.refresh_cameras()
        return self._mgr.stop(name)
