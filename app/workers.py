"""Camera worker processes: the runtime inside each worker and the manager in the dashboard.

Protocol (files under config.RUN_DIR, so CLI-started workers are visible too):
- <camera>.json  status written by the worker every WORKER_STATUS_SECONDS:
                 pid, source, started, updated, state (loading/running/stopped), fps, reason.
- <camera>.stop  created by the manager to ask the worker to exit cleanly.
A worker started by the dashboard also gets --parent-pid and exits by itself
if the dashboard process is gone, so stopping the dashboard leaves no orphans.
Worker output goes to config.LOG_DIR/<camera>.log.
"""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import config
from app.camera import is_webcam, parse_source

STILL_ACTIVE = 259  # Windows GetExitCodeProcess value for a running process


# --- Processes -------------------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # never on Windows: there os.kill terminates the process
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def python_command(python: str, env: dict) -> str:
    """Interpreter to launch, so that Popen.pid is the worker itself.

    On Windows, .venv\\Scripts\\python.exe is a launcher that runs the real
    interpreter as a child process; killing it would orphan the worker. Like
    the standard library's multiprocessing (bpo-35797), start the base
    interpreter directly and tell it which venv to use.
    """
    base = getattr(sys, "_base_executable", None)
    if os.name == "nt" and base and Path(python) == Path(sys.executable) and Path(base) != Path(sys.executable):
        env["__PYVENV_LAUNCHER__"] = sys.executable
        return base
    return python


def _write_json(path: Path, data: dict) -> None:
    """Atomic write; retries because Windows refuses a replace while a reader has the file open."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status_path(camera: str, run_dir: Path | None = None) -> Path:
    return Path(run_dir or config.RUN_DIR) / f"{camera}.json"


def stop_path(camera: str, run_dir: Path | None = None) -> Path:
    return Path(run_dir or config.RUN_DIR) / f"{camera}.stop"


def live_status(camera: str, run_dir: Path | None = None) -> dict | None:
    """The camera's status if its worker process is alive and hasn't reported stopping, else None."""
    data = _read_json(status_path(camera, run_dir))
    if data and data.get("state") != "stopped" and pid_alive(data.get("pid")):
        return data
    return None


def running_elsewhere(camera: str, run_dir: Path | None = None) -> int | None:
    """PID of another live, responsive worker for this camera (check before starting one)."""
    data = live_status(camera, run_dir)
    if data and data.get("pid") != os.getpid() and time.time() - data.get("updated", 0) < config.WORKER_STALE_SECONDS:
        return data["pid"]
    return None


def webcam_conflict(camera: str, source, cameras: list[dict], run_dir: Path | None = None) -> str | None:
    """Name of another live worker that uses the same webcam index as `source`, if any."""
    source = parse_source(str(source))
    if not is_webcam(source):
        return None
    for other in cameras:
        if other["name"] == camera:
            continue
        status = live_status(other["name"], run_dir)
        if status is None:
            continue
        used = status.get("source")
        used = parse_source(str(used if used is not None else other["source"]))
        if used == source:
            return other["name"]
    return None


# --- Inside a worker -------------------------------------------------------------


class WorkerRuntime:
    """Status file, stop file and parent watchdog for one worker process."""

    def __init__(self, camera: str, source, parent_pid: int | None = None, run_dir: Path | None = None):
        self.camera = camera
        self.source = source if source is None or isinstance(source, int) else str(source)
        self.parent_pid = parent_pid
        self.run_dir = Path(run_dir or config.RUN_DIR)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.status_file = status_path(camera, self.run_dir)
        self.stop_file = stop_path(camera, self.run_dir)
        self.stop_file.unlink(missing_ok=True)  # a stale request from an earlier run
        self.started = datetime.now()
        self.frames = 0
        self.fps = 0.0
        self._window_frames = 0
        self._window_start = self._last_write = self._last_check = time.monotonic()
        self._stop_reason = None
        self.write("loading")

    def write(self, state: str, reason: str | None = None) -> None:
        _write_json(self.status_file, {
            "camera": self.camera, "pid": os.getpid(), "source": self.source,
            "started": self.started.isoformat(timespec="seconds"), "updated": time.time(),
            "state": state, "fps": round(self.fps, 1), "frames": self.frames, "reason": reason,
        })

    def tick(self, processed: bool = False) -> None:
        """Call once per loop iteration; counts processed frames and refreshes the status file."""
        if processed:
            self.frames += 1
            self._window_frames += 1
        now = time.monotonic()
        if now - self._last_write >= config.WORKER_STATUS_SECONDS:
            self.fps = self._window_frames / (now - self._window_start)
            self._window_frames, self._window_start = 0, now
            self._last_write = now
            self.write("running")

    def should_stop(self) -> str | None:
        """Reason to stop ("stop requested" or "dashboard exited"), checked at most twice a second."""
        now = time.monotonic()
        if self._stop_reason is None and now - self._last_check >= 0.5:
            self._last_check = now
            if self.stop_file.exists():
                self._stop_reason = "stop requested"
            elif self.parent_pid and not pid_alive(self.parent_pid):
                self._stop_reason = "dashboard exited"
        return self._stop_reason

    def close(self, reason: str) -> None:
        self.write("stopped", reason)
        self.stop_file.unlink(missing_ok=True)


# --- In the dashboard -------------------------------------------------------------


class WorkerError(Exception):
    pass


@dataclass
class WorkerStatus:
    camera: str
    state: str  # "running", "starting", "not responding" or "stopped"
    pid: int | None = None
    started: datetime | None = None
    fps: float | None = None
    source: str | int | None = None
    managed: bool = False  # started by this dashboard process
    detail: str = ""  # stop reason or exit code of the last run


class WorkerManager:
    """Starts, stops and reports on one recognize.py process per camera."""

    def __init__(self, cameras: list[dict] | None = None, script: Path | None = None,
                 extra_args: tuple[str, ...] = ("--no-window",), run_dir: Path | None = None,
                 log_dir: Path | None = None, python: str | None = None):
        self._cameras = cameras  # None = config.CAMERAS (read each time)
        self.script = Path(script or config.ROOT_DIR / "scripts" / "recognize.py")
        self.extra_args = tuple(extra_args)
        self._run_dir, self._log_dir = run_dir, log_dir
        self.python = python or sys.executable
        self._procs: dict[str, subprocess.Popen] = {}
        self._exit: dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def cameras(self) -> list[dict]:
        return self._cameras if self._cameras is not None else config.CAMERAS

    @property
    def run_dir(self) -> Path:
        return Path(self._run_dir or config.RUN_DIR)

    @property
    def log_dir(self) -> Path:
        return Path(self._log_dir or config.LOG_DIR)

    def camera(self, name: str) -> dict:
        for cam in self.cameras:
            if cam["name"] == name:
                return cam
        raise WorkerError(f"unknown camera {name!r}")

    def log_path(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def _proc(self, name: str) -> subprocess.Popen | None:
        """Our process for this camera if it's still running; records the exit code otherwise."""
        proc = self._procs.get(name)
        if proc is not None and proc.poll() is not None:
            self._exit[name] = f"exit code {proc.returncode}"
            del self._procs[name]
            return None
        return proc

    def status(self, name: str) -> WorkerStatus:
        with self._lock:
            cam = self.camera(name)
            proc = self._proc(name)
            data = _read_json(status_path(name, self.run_dir))
            alive = data is not None and data.get("state") != "stopped" and pid_alive(data.get("pid"))
            fresh = alive and time.time() - data.get("updated", 0) <= config.WORKER_STALE_SECONDS
            if proc is not None and not (alive and data.get("pid") == proc.pid):
                return WorkerStatus(name, "starting", proc.pid, source=cam["source"], managed=True)
            if alive:
                state = ("running" if data.get("state") == "running" else "starting") if fresh else "not responding"
                return WorkerStatus(
                    name, state, data["pid"], datetime.fromisoformat(data["started"]), data.get("fps"),
                    cam["source"] if data.get("source") is None else data["source"], managed=proc is not None,
                )
            detail = (data or {}).get("reason") or self._exit.get(name, "")
            return WorkerStatus(name, "stopped", source=cam["source"], detail=detail)

    def statuses(self) -> list[WorkerStatus]:
        return [self.status(c["name"]) for c in self.cameras]

    def webcam_users(self) -> list[str]:
        """Cameras whose live worker is using a webcam (the enrollment camera needs it free)."""
        users = []
        for st in self.statuses():
            if st.state != "stopped" and is_webcam(parse_source(str(st.source))):
                users.append(st.camera)
        return users

    def start(self, name: str) -> WorkerStatus:
        with self._lock:
            cam = self.camera(name)
            if self.status(name).state != "stopped":
                raise WorkerError(f"{name} is already running")
            other = webcam_conflict(name, cam["source"], self.cameras, self.run_dir) or self._starting_on(name)
            if other:
                raise WorkerError(f"webcam {cam['source']} is already in use by the {other} worker; stop it first")

            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = self.log_path(name)
            if log.exists() and log.stat().st_size > config.WORKER_LOG_MAX_BYTES:
                os.replace(log, log.with_name(log.name + ".1"))
            stop_path(name, self.run_dir).unlink(missing_ok=True)

            env = os.environ.copy()
            env.pop(config.DASHBOARD_PASSWORD_ENV, None)  # workers don't need the admin password
            cmd = [python_command(self.python, env), "-u", str(self.script), "--camera", name, *self.extra_args,
                   "--parent-pid", str(os.getpid())]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} started from the dashboard =====\n")
                f.flush()
                proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        cwd=config.ROOT_DIR, env=env, creationflags=flags)
            self._procs[name] = proc
            self._exit.pop(name, None)
            return self.status(name)

    def _starting_on(self, name: str) -> str | None:
        """Another of our workers on the same webcam that is still loading (no status file yet)."""
        source = parse_source(str(self.camera(name)["source"]))
        if not is_webcam(source):
            return None
        for other in list(self._procs):
            if other != name and self._proc(other) and parse_source(str(self.camera(other)["source"])) == source:
                return other
        return None

    def stop(self, name: str, timeout: float | None = None) -> str:
        """Ask the worker to exit cleanly; kill it after `timeout` only if this dashboard started it."""
        timeout = config.WORKER_STOP_TIMEOUT if timeout is None else timeout
        with self._lock:
            st = self.status(name)
            if st.state == "stopped":
                return "not running"
            proc = self._proc(name)
            stop_path(name, self.run_dir).touch()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (proc.poll() is not None) if proc else not pid_alive(st.pid):
                with self._lock:
                    self._proc(name)
                return "stopped cleanly"
            time.sleep(0.1)
        if proc is None:
            return f"did not stop within {timeout:.0f} s; end process {st.pid} manually"
        proc.kill()
        proc.wait(5)
        with self._lock:
            self._proc(name)
            with open(self.log_path(name), "a", encoding="utf-8") as f:
                f.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} killed after {timeout:.0f} s =====\n")
        return f"killed after {timeout:.0f} s"

    def stop_all(self) -> None:
        """Stop every worker this dashboard started (called when the dashboard exits)."""
        for name in list(self._procs):
            try:
                self.stop(name)
            except Exception:  # noqa: BLE001  best effort at shutdown
                pass

    def log_tail(self, name: str, lines: int | None = None) -> list[str]:
        lines = lines or config.WORKER_LOG_LINES
        path = self.log_path(name)
        if not path.exists():
            return []
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200 * lines))
            text = f.read().decode("utf-8", errors="replace")
        return text.splitlines()[-lines:]
