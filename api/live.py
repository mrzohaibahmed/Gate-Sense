"""Live video and live events for the API.

FrameHub: one Channel per camera being streamed. The camera worker (another
process) writes its annotated frame, JPEG-encoded once, to data/live/<camera>.jpg;
while at least one client watches, the channel's thread keeps <camera>.watch fresh
(so the worker writes every processed frame, up to STREAM_FPS) and reads each new
frame once. Every client gets the same bytes: more viewers never mean more
encoding. The thread stops, and the .watch file goes, with the last viewer.

EventHub: the WebSocket clients of /api/ws. While any are connected, one task
looks for new events and corrections (in the database, so those from the camera
workers and scripts are seen too) every WS_POLL_SECONDS, sends each
camera's latest recognition decisions (from data/live/<camera>.json) when they
change, only to clients with live:decisions (admins), and sends
every camera's status every WS_CAMERA_SECONDS, when it also closes clients whose
session ended. Each client has a bounded queue; one that falls WS_QUEUE messages
behind is disconnected instead of slowing the others.
"""

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from starlette.concurrency import run_in_threadpool

from api.db import Database
from api.schemas import event_out
from app import config, live

CORRECTIONS = ("add_event", "edit_event", "void_event", "restore_event")
DECISIONS = "live:decisions"
SESSION_ENDED = 4401  # WebSocket close code: log in again


def fit_width(jpeg: bytes, width: int) -> bytes:
    """The JPEG shrunk to `width` if it is wider (normally it isn't: the worker draws it at most STREAM_MAX_WIDTH)."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.shape[1] <= width:
        return jpeg
    img = cv2.resize(img, (width, round(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, config.LIVE_JPEG_QUALITY])
    return buf.tobytes() if ok else jpeg


def read_frame(camera: str, live_dir: Path) -> bytes | None:
    """The camera's newest annotated frame (no one streaming needed), within STREAM_MAX_WIDTH."""
    try:
        data = live.frame_path(camera, live_dir).read_bytes()
    except OSError:
        return None
    return fit_width(data, config.STREAM_MAX_WIDTH) if data else None


class Channel:
    """The newest frame of one camera, shared by all its viewers; a thread reads it while anyone watches."""

    def __init__(self, camera: str, live_dir: Path):
        self.camera, self.live_dir = camera, live_dir
        self.viewers = 0
        self.seq = 0  # bumped for each new frame
        self.frame: bytes | None = None
        self.decoded = 0  # frames read (and, if too wide, re-encoded): once per frame, whatever the viewers
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def latest(self) -> tuple[int, bytes] | None:
        with self._lock:
            return (self.seq, self.frame) if self.frame is not None else None

    def join(self) -> None:
        with self._lock:
            self.viewers += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"stream-{self.camera}", daemon=True)
                self._thread.start()

    def leave(self) -> None:
        with self._lock:
            self.viewers = max(0, self.viewers - 1)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None

    def _run(self) -> None:
        path = live.frame_path(self.camera, self.live_dir)
        last_mtime, last_touch = None, 0.0
        try:
            while True:
                with self._lock:
                    if self.viewers == 0:
                        self._thread = None
                        return
                now = time.time()
                if now - last_touch >= 1.0:
                    live.mark_watched(self.camera, self.live_dir)
                    last_touch = now
                try:
                    mtime = path.stat().st_mtime_ns
                    if mtime != last_mtime:
                        data = path.read_bytes()
                        last_mtime = mtime
                        if data:
                            data = fit_width(data, config.STREAM_MAX_WIDTH)
                            with self._lock:
                                self.frame, self.seq, self.decoded = data, self.seq + 1, self.decoded + 1
                except OSError:
                    pass  # no frame yet, or the worker is replacing it this instant
                time.sleep(1.0 / (2 * config.STREAM_FPS))
        finally:
            with self._lock:
                idle = self.viewers == 0
            if idle:
                live.unwatch(self.camera, self.live_dir)


class FrameHub:
    def __init__(self, live_dir):
        self._live_dir = live_dir  # callable: the worker manager's live folder
        self._channels: dict[str, Channel] = {}
        self._lock = threading.Lock()

    def channel(self, camera: str) -> Channel:
        with self._lock:
            ch = self._channels.get(camera)
            if ch is None:
                ch = self._channels[camera] = Channel(camera, Path(self._live_dir()))
            return ch


# --- WebSocket ------------------------------------------------------------------------------------


@dataclass(eq=False)
class Client:
    """Create it inside the WebSocket handler: it remembers that event loop, so the hub can deliver from any loop."""
    websocket: object
    username: str
    session_id: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(config.WS_QUEUE))
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_running_loop)
    permissions: frozenset = frozenset()  # the user's, for messages only some roles get
    closed: int | None = None  # close code, once the hub drops it


class EventHub:
    def __init__(self, db: Database, workers):
        self.db, self.workers = db, workers
        self.clients: set[Client] = set()
        self._last: tuple[int, int] | None = None  # (event id, audit id) already sent; None = start from now
        self._camera_due = 0.0
        self._decisions: dict[str, str] = {}  # camera -> the decision list last sent (as JSON)
        self._camera_ids: dict[str, int] = {}

    def join(self, client: Client) -> None:
        self.clients.add(client)
        self._camera_due = 0.0  # camera statuses at the next tick, so a new client sees them at once
        if DECISIONS in client.permissions:
            self._decisions = {}  # and every camera's decisions

    def leave(self, client: Client) -> None:
        self.clients.discard(client)

    def drop(self, client: Client, code: int) -> None:
        """Stop sending to a client; its sender closes the socket with `code`."""
        if client.closed is None:
            client.closed = code
            self.clients.discard(client)
            self._deliver(client, None)

    def broadcast(self, message: dict, permission: str | None = None) -> None:
        """To every client, or only to those whose role has `permission`."""
        for client in list(self.clients):
            if permission is None or permission in client.permissions:
                self._deliver(client, message)

    def _deliver(self, client: Client, message: dict | None) -> None:
        """Queue a message in the client's own event loop (asyncio queues aren't thread-safe). Under uvicorn that is
        this loop; under a test client every request has its own."""
        def put() -> None:
            if message is None:  # the close signal must get through: make room for it
                while client.queue.full():
                    client.queue.get_nowait()
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                self.drop(client, 1013)  # "try again later": far behind

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if client.loop is running:
            put()
        elif not client.loop.is_closed():
            client.loop.call_soon_threadsafe(put)

    async def run(self) -> None:
        """The polling loop (the API's lifespan starts and cancels it)."""
        while True:
            await asyncio.sleep(config.WS_POLL_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  a bad tick must not end live updates
                print(f"live updates: {type(e).__name__}: {e}")

    async def tick(self) -> None:
        if not self.clients:
            self._last = None
            return
        if self._last is None:
            self._last = await run_in_threadpool(self.db.last_ids)
        event_id, audit_id = self._last
        rows = await run_in_threadpool(self.db.events_after, event_id)
        repeats = await run_in_threadpool(self.db.repeat_ids, rows) if rows else set()
        for row in rows:
            self.broadcast({"type": "event", **event_out(row, repeats).model_dump(mode="json")})
            event_id = row["id"]
        rows, audit_id = await run_in_threadpool(self.db.audit_after, audit_id, CORRECTIONS)
        for row in rows:
            self.broadcast({"type": "attendance_changed", "action": row["action"], "target": row["target"],
                            "user": row["user"], "reason": row["reason"]})
        self._last = (event_id, audit_id)
        if time.monotonic() >= self._camera_due:
            self._camera_due = time.monotonic() + config.WS_CAMERA_SECONDS
            await self.cameras_and_sessions()  # first: it also learns the camera ids the decisions carry
        if any(DECISIONS in c.permissions for c in self.clients):
            await self.decisions()

    async def cameras_and_sessions(self) -> None:
        from api import auth

        alive = await run_in_threadpool(self.db.sessions_alive, [c.session_id for c in self.clients], auth.now())
        for client in list(self.clients):
            if client.session_id not in alive:
                self.drop(client, SESSION_ENDED)
        if not self.clients:
            return
        statuses = await run_in_threadpool(self.workers.statuses)
        ids = {c["name"]: c["id"] for c in await run_in_threadpool(self.db.cameras, self.workers.cameras)}
        self._camera_ids = ids
        for info in statuses:
            self.broadcast({
                "type": "camera", "id": ids.get(info.camera), "name": info.camera, "status": info.state,
                "fps": info.fps, "error": info.last_error, "warning": info.warning, "held_by": info.held_by,
                "last_frame_at": info.last_frame_at.isoformat() if info.last_frame_at else None})

    async def decisions(self) -> None:
        """Each camera's latest decisions, when they changed since last sent (to live:decisions clients only)."""
        names = [c["name"] for c in self.workers.cameras]
        found = await run_in_threadpool(lambda: {n: live.read_info(n, self.workers.live_dir).get("decisions", [])
                                                 for n in names})
        for name, rows in found.items():
            key = json.dumps(rows, sort_keys=True)
            if self._decisions.get(name) == key:
                continue
            self._decisions[name] = key
            self.broadcast({"type": "decisions", "camera": name, "id": self._camera_ids.get(name),
                            "decisions": [decision_out(d) for d in rows]}, DECISIONS)


def decision_out(d: dict) -> dict:
    """A worker's decision (app/live.py LiveWriter.decision) with the margin over the second-best person."""
    score, second = d.get("score"), d.get("second")
    return {"time": d.get("time"), "kind": d.get("kind"), "track": d.get("track"), "name": d.get("name"),
            "score": score, "second": second,
            "margin": round(score - second, 3) if score is not None and second is not None else None,
            "face_px": d.get("face_px"), "liveness": d.get("liveness"), "detail": d.get("detail") or ""}
