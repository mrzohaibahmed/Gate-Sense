"""Live video (MJPEG stream, snapshot) and live events (WebSocket /api/ws). See api/live.py for how frames and
events are shared."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from api import auth
from api.auth import CurrentUser, require_permission
from api.live import Client, read_frame
from api.routes.cameras import get_or_404
from app import config

router = APIRouter(tags=["live"], responses=auth.PROTECTED_RESPONSES)
view = require_permission("live:view")
BOUNDARY = "frame"


def part(jpeg: bytes) -> bytes:
    return (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
            + jpeg + b"\r\n")


@router.get("/api/cameras/{camera_id}/stream", response_class=StreamingResponse, responses={
    200: {"content": {f"multipart/x-mixed-replace; boundary={BOUNDARY}": {}},
          "description": "MJPEG: the worker's annotated frames (face boxes, names), up to STREAM_FPS a second and "
                         "STREAM_MAX_WIDTH wide; use it as an <img> src"},
    409: {"description": "the camera isn't running"}})
async def stream_camera(camera_id: int, request: Request,
                        max_frames: int | None = Query(None, ge=1, description="end after this many frames"),
                        _: CurrentUser = Depends(view)) -> StreamingResponse:
    """Live video of a running camera. Every viewer gets the same encoded frames, so viewers don't slow down
    recognition; when the last one leaves, the worker goes back to writing a frame every LIVE_FRAME_SECONDS. The
    stream ends when the camera stops."""
    cam = await run_in_threadpool(get_or_404, request, camera_id)
    if cam["worker"].state == "stopped":
        raise HTTPException(409, f"{cam['name']} isn't running: start it first.")
    name, manager = cam["name"], request.app.state.workers
    channel = request.app.state.frames.channel(name)

    async def frames():
        channel.join()
        try:
            sent, count, next_due, next_check = 0, 0, 0.0, time.monotonic() + 2
            while True:
                now = time.monotonic()
                got = channel.latest()
                if got and got[0] != sent and now >= next_due:
                    sent, next_due = got[0], now + 1.0 / config.STREAM_FPS
                    yield part(got[1])
                    count += 1
                    if max_frames and count >= max_frames:
                        return
                if now >= next_check:
                    next_check = now + 2
                    if (await run_in_threadpool(manager.status, name)).state == "stopped":
                        return
                await asyncio.sleep(1.0 / (4 * config.STREAM_FPS))
        finally:
            channel.leave()

    return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/cameras/{camera_id}/snapshot.jpg", response_class=Response, responses={
    200: {"content": {"image/jpeg": {}}, "description": "the newest annotated frame"},
    404: {"description": "no frame: the camera isn't running or hasn't sent one yet"}})
def camera_snapshot(camera_id: int, request: Request, _: CurrentUser = Depends(view)) -> Response:
    """One current frame (annotated) of a running camera."""
    cam = get_or_404(request, camera_id)
    channel = request.app.state.frames.channel(cam["name"])
    got = channel.latest() if channel.running else None
    jpeg = got[1] if got else read_frame(cam["name"], request.app.state.workers.live_dir)
    if jpeg is None:
        raise HTTPException(404, f"No frame from {cam['name']}: is it running?")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.websocket("/api/ws")
async def live_updates(websocket: WebSocket) -> None:
    """Live updates as JSON messages, after the session cookie is checked:

    - {"type": "hello", "user", "role"} once;
    - {"type": "event", ...an Event} for each new IN/OUT (camera or manual);
    - {"type": "camera", "id", "name", "status", "fps", "error", "warning", "held_by", "last_frame_at"} for every
      camera every WS_CAMERA_SECONDS;
    - {"type": "attendance_changed", "action", "target", "user", "reason"} after a correction (API or dashboard);
    - admins only (live:decisions): {"type": "decisions", "camera", "id", "decisions": [{"time", "kind" (match,
      unknown, skip, event, cool-down, not saved, closed, visitor), "track", "name", "score", "second", "margin", "face_px"
      (camera pixels), "liveness" (the frame's live score 0-1, null if not checked), "detail" (vote or skip reason,
      e.g. "spoof (0.12)"; visitor: an unknown visitor recorded)}]} with a camera's latest decisions whenever they
      change;
    - admins and guards only (unknowns:read): {"type": "unknown", ...an UnknownVisit} for each new unknown visitor,
      sent while they are still in view, and {"type": "unknowns_changed", "action", "target", "user"} after a review,
      the visitor log switched, or old visits deleted.

    Refused (policy violation, 1008) without a valid session, without attendance:read, or from another site's page;
    closed with 4401 when the session ends.
    """
    result = await run_in_threadpool(auth.websocket_user, websocket, "attendance:read")
    if isinstance(result, str):
        await websocket.close(code=1008, reason=result)
        return
    user, session_id = result
    await websocket.accept()
    hub = websocket.app.state.events
    client = Client(websocket, user.username, session_id, permissions=frozenset(user.permissions))
    hub.join(client)
    await websocket.send_json({"type": "hello", "user": user.username, "role": user.role})

    async def send() -> None:
        while True:
            message = await client.queue.get()
            if message is None:
                await websocket.close(code=client.closed or 1000)
                return
            await websocket.send_json(message)

    async def receive() -> None:  # clients needn't send anything; this notices when they go
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass

    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.exception()  # retrieved: a client going away mid-send is normal
    finally:
        for task in tasks:
            task.cancel()
        hub.leave(client)
