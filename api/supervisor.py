"""Keeps enabled cameras recording: starts them when the app starts and restarts any that stops.

A camera with "enabled" in cameras.json (Settings > Cameras, "Auto") is started at the app's first check, and
whenever its worker stops for any reason other than a stop request (a crash, "lost the camera", a killed process,
the app that started it gone) it is started again after SUPERVISOR_BACKOFF seconds: 5, 10, 20, ... up to 5 minutes,
back to 5 s once it has run for SUPERVISOR_STABLE_SECONDS. A start that is refused (a missing camera password, the
webcam held by the enrollment wizard) is retried on the same schedule.

A stop that someone asked for is respected: Stop on the Live cameras page, stop.bat or the command line (a worker
whose stop reason is "stop requested") pauses the camera until it is started again by hand, or the app restarts.
Automatic starts are audited as user "system" (start_camera, with "automatic": true).
"""

import asyncio
import threading
import time
from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool

from api.workers import CameraBusy, WorkerManager
from app import config
from app.workers import WorkerError

STOP_REQUESTED = "stop requested"


@dataclass
class Watch:
    """What the supervisor knows about one camera."""
    paused: bool = False  # stopped on request: left alone until started by hand (or the app restarts)
    restarts: int = 0  # automatic restarts since it last ran SUPERVISOR_STABLE_SECONDS (picks the backoff delay)
    next_try: float = 0.0  # time.monotonic() of the next automatic start
    running: bool = False  # last seen running (or just started)
    running_since: float | None = None
    problem: str | None = None  # the last refused start, printed once
    auto_on: bool = False  # Auto was just turned on: the next start says so in the audit log


class Supervisor:
    def __init__(self, workers: WorkerManager, database=None, clock=time.monotonic, log=None):
        self.workers, self.database = workers, database
        self.clock = clock
        self.log = log or (lambda message: print(message, flush=True))
        self._watches: dict[str, Watch] = {}
        self._lock = threading.RLock()  # held while a camera is checked and started
        self._closed = False

    def watch(self, name: str) -> Watch:
        with self._lock:
            return self._watches.setdefault(name, Watch())

    def delay(self, w: Watch) -> float:
        backoff = config.SUPERVISOR_BACKOFF
        return float(backoff[min(w.restarts, len(backoff) - 1)])

    # --- Told by the API ------------------------------------------------------------------------------------

    def started_by_hand(self, name: str) -> None:
        """Start on the Live cameras page: watched again from a clean slate."""
        with self._lock:
            w = self.watch(name)
            w.paused, w.restarts, w.problem = False, 0, None
            w.running, w.running_since = True, self.clock()

    def stopped_by_hand(self, name: str) -> None:
        """Stop on the Live cameras page: stays stopped until started again (call before stopping the worker)."""
        with self._lock:
            w = self.watch(name)
            w.paused, w.running, w.running_since = True, False, None

    def enabled_changed(self, name: str, enabled: bool) -> None:
        """Auto turned on: start it at the next check (if stopped). Turned off: no more automatic starts."""
        with self._lock:
            w = self.watch(name)
            if enabled:
                w.paused, w.restarts, w.next_try, w.problem, w.auto_on = False, 0, 0.0, None, True
                w.running, w.running_since = False, None  # seen afresh at the next check

    def close(self) -> None:
        """No more automatic starts (the app is stopping). Waits for a check that is starting a camera right now, so
        the shutdown that follows sees that worker too."""
        with self._lock:
            self._closed = True

    def paused(self, name: str) -> bool:
        with self._lock:
            return name in self._watches and self._watches[name].paused

    # --- The check ------------------------------------------------------------------------------------------

    def tick(self) -> list[str]:
        """One check of every enabled camera; returns the cameras started (for tests)."""
        started = []
        cameras = self.workers.cameras
        with self._lock:
            for gone in set(self._watches) - {c["name"] for c in cameras}:
                del self._watches[gone]
        for cam in cameras:
            name = cam["name"]
            if not cam.get("enabled"):
                continue
            try:
                if self._check(name):
                    started.append(name)
            except Exception as e:  # noqa: BLE001  one camera's problem must not stop the others' checks
                self.log(f"Camera {name}: supervisor check failed: {type(e).__name__}: {e}")
        return started

    def _check(self, name: str) -> bool:
        now = self.clock()
        info = self.workers.status(name)
        with self._lock:
            if self._closed:
                return False
            w = self.watch(name)
            if info.state != "stopped":
                if not w.running or w.running_since is None:
                    w.running, w.running_since = True, now
                if w.restarts and now - w.running_since >= config.SUPERVISOR_STABLE_SECONDS:
                    w.restarts = 0  # healthy again: the next restart waits the shortest delay
                return False
            if w.running:  # it was running and has stopped
                w.running, w.running_since = False, None
                if info.detail == STOP_REQUESTED:
                    w.paused = True
                    self.log(f"Camera {name}: stopped on request; it stays stopped until it is started again")
                    return False
                delay = self.delay(w)
                w.next_try = now + delay
                self.log(f"Camera {name} stopped ({info.detail or info.last_error or 'no reason given'}); "
                         f"starting it again in {delay:g} s")
                return False
            if w.paused or now < w.next_try:
                return False
            if w.auto_on:
                reason = "enabled: started when Auto was turned on"
            elif w.restarts or w.next_try:
                reason = "enabled: restarted after it stopped" + (f" ({info.detail})" if info.detail else "")
            else:
                reason = "enabled: started with GateSense"
            try:
                started = self.workers.start(name)
            except (CameraBusy, WorkerError) as e:
                delay = self.delay(w)
                w.next_try = now + delay
                w.restarts += 1
                if str(e) != w.problem:
                    w.problem = str(e)
                    self.log(f"Camera {name} can't start yet: {e}; trying again in {delay:g} s (and after that "
                             "on the same schedule, up to every 5 minutes)")
                return False
            if w.next_try:
                w.restarts += 1
            w.running, w.running_since, w.problem, w.auto_on = True, now, None, False
        self.log(f"Camera {name} started automatically (process {started.pid}): {reason}")
        self._audit(name, started.pid, reason)
        return True

    def _audit(self, name: str, pid: int | None, reason: str) -> None:
        if self.database is None:
            return
        try:
            self.database.audit("system", "start_camera", f"camera:{name}", {"pid": pid, "automatic": True}, reason)
        except Exception as e:  # noqa: BLE001  the camera runs; a missing audit row is reported, not fatal
            self.log(f"Camera {name}: could not write the audit row: {type(e).__name__}: {e}")

    async def run(self) -> None:
        """The lifespan task: a check now, then every SUPERVISOR_CHECK_SECONDS."""
        while True:
            try:
                await run_in_threadpool(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  never end the app over this
                self.log(f"Camera supervisor: {type(e).__name__}: {e}")
            await asyncio.sleep(config.SUPERVISOR_CHECK_SECONDS)
