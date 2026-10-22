"""Camera workers for the API.

Wraps app.workers.WorkerManager (the same recognize.py processes and the same
status/stop files under data/run/ that the Streamlit dashboard uses), and adds
what the API needs:

- who holds a camera: this API, the Streamlit dashboard or the command line
  (from the worker's --parent-pid in its status file);
- a clear CameraBusy error instead of opening a camera twice: a camera that
  any process already runs, a webcam another camera's worker uses, or the
  webcam while the dashboard's Enrollment camera is switched on. The worker
  itself also takes an OS lock per camera and webcam (app.workers.acquire_camera),
  so even two starts at the same moment can't both open it;
- fps, last frame time and last error per worker;
- open()/close() for the FastAPI lifespan: data/run/_api.json while running,
  and on shutdown every worker this API started is asked to stop, all at once.
"""

import json
import os
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
CLEAN_STOPS = {"stop requested", "end of video", "window closed", "interrupted", "dashboard exited", "exit code 0"}


class CameraBusy(WorkerError):
    """The camera (or its webcam) is already held by a worker or by the dashboard's Enrollment camera."""


class WorkerInfo(BaseModel):
    camera: str
    state: str  # running, starting, not responding, stopped
    held_by: str | None = None  # api, dashboard, command line, other; None when stopped
    pid: int | None = None
    started: datetime | None = None
    fps: float | None = None
    last_frame_at: datetime | None = None
    last_error: str | None = None
    warning: str | None = None
    detail: str = ""  # stop reason of the last run


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
        if st.parent_pid in app_workers.dashboard_pids(self.run_dir):
            return "dashboard"
        return "other"

    def _describe(self, st: app_workers.WorkerStatus) -> str:
        return {
            "api": "started by this API",
            "dashboard": "started from the Streamlit dashboard's Cameras page",
            "command line": "started from the command line (scripts\\recognize.py)",
        }.get(self.holder(st), f"started by another GateSense process ({st.parent_pid})")

    def info(self, st: app_workers.WorkerStatus) -> WorkerInfo:
        health = st.health or {}
        frame_time = health.get("last_frame_at")
        if frame_time is None and st.state != "stopped":
            frame_time = live.read_info(st.camera, self._mgr.live_dir).get("frame_time")
        error = health.get("last_error")
        if st.state == "stopped" and st.detail and st.detail not in CLEAN_STOPS:
            error = error or st.detail
        return WorkerInfo(
            camera=st.camera, state=st.state, held_by=self.holder(st), pid=st.pid, started=st.started,
            fps=st.fps, last_frame_at=datetime.fromtimestamp(frame_time) if frame_time else None,
            last_error=error, warning=st.warning, detail=st.detail,
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
        st = self._mgr.status(name)  # WorkerError for an unknown camera
        if st.state != "stopped":
            raise CameraBusy(f"{name} is already running ({self._describe(st)}, process {st.pid}); "
                             "it can't be opened twice")
        source = self._mgr.camera(name)["source"]
        if is_webcam(parse_source(str(source))):
            if other := app_workers.webcam_conflict(name, source, self._mgr.cameras, self.run_dir):
                raise CameraBusy(f"webcam {source} is already in use by the {other} worker "
                                 f"({self._describe(self._mgr.status(other))}); stop it first")
            if app_workers.enrollment_camera_claimed(self.run_dir):
                raise CameraBusy(f"webcam {source} is in use by the Streamlit dashboard's Enrollment camera; "
                                 "switch off \"Use the camera\" there first")
        return self.info(self._mgr.start(name))

    def stop(self, name: str) -> str:
        """Ask the worker to stop (whoever started it); returns the result text."""
        config.refresh_cameras()
        return self._mgr.stop(name)
