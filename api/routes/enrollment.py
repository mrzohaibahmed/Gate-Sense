"""Guided enrollment from the web dashboard: hold the webcam, check a camera frame, review the captured photos,
save them.

The rules are app/enroll_guide.py and app/enroll_quality.py (the same as scripts/enroll.py): the browser sends
a frame every few hundred milliseconds to /api/enrollment/check for the current step (front, slightly left,
slightly right) and enables Capture only when it comes back ok; /api/enrollment/review checks the captured
photos together (do they match each other, do they look like someone else); /api/people/{id}/enroll saves them
to known/<code>/ with their step and rebuilds the person's embeddings. Frames are raw camera images (not
mirrored): only the preview the person sees is mirrored.

While the wizard has the webcam open it holds a claim (/api/enrollment/webcam), so webcam camera workers can't
start, and it can't take the webcam while one runs; it renews the claim while open and releases it when it closes.

With an IP camera instead (/api/enrollment/sources, then /api/enrollment/sessions), the server reads the camera
(sharing a running worker's frames, see api/enroll_camera.py), serves a live preview (MJPEG) and runs the same
checks on the camera's full-resolution enrollment window; Capture returns that window as the photo. The session is
held, renewed and released like the webcam claim. The user's last choice is remembered (user_preferences).
"""

import asyncio
import base64
import binascii
import time
from typing import Literal

import cv2
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import auth
from api.auth import CurrentUser, require_permission
from api.enroll_camera import window
from api.routes import cameras as camera_routes
from api.routes.live import BOUNDARY, part
from api.routes.people import _enroll_lock, get_or_404
from api.schemas import OptionalReason
from api.workers import CameraBusy
from app import config, enroll_guide, enrollment
from app.workers import WorkerError

router = APIRouter(tags=["enrollment"], responses=auth.PROTECTED_RESPONSES)
manage = require_permission("people:manage")
MAX_BYTES = 6_000_000  # one frame or photo


class StepInfo(BaseModel):
    key: str
    title: str
    instruction: str
    yaw: tuple[float, float] = Field(description="degrees, the person's own point of view: + = turned to their left")
    pitch: tuple[float, float]
    required: bool


class FrameCheck(BaseModel):
    ok: bool = Field(description="the frame is good for this step: enable Capture")
    state: str = Field(description='"none" (no face), "bad" (a face, but not right yet) or "good"')
    message: str = Field(description='what to tell the person, e.g. "Move closer" or "Good, hold still"')
    yaw: float | None = Field(description="degrees, + = turned to their own left")
    pitch: float | None
    face_px: int | None
    score: float | None = Field(description="photo quality 0-100")
    box: list[float] | None = Field(description="the face: [x1, y1, x2, y2] as fractions of the frame")
    guide: list[float] = Field(description="the guide box the face must be in, as fractions of the frame")


class PhotoIn(BaseModel):
    step: str
    image: str = Field(description="the captured frame: JPEG, base64 (a data: URL prefix is allowed)")


class PhotosIn(BaseModel):
    photos: list[PhotoIn] = Field(min_length=1, max_length=10)


class PhotosSave(PhotosIn, OptionalReason):
    source: str | None = Field(None, max_length=64, description='where the photos came from: "webcam" or the '
                                                                  'camera name (for the audit log)')


class PhotoReview(BaseModel):
    step: str
    ok: bool
    problem: str | None = Field(description="why this photo can't be used (retake it)")
    warnings: list[str] = Field(description="it can be saved, but check: doesn't match the others, looks like someone")
    yaw: float | None
    pitch: float | None
    score: float | None


class Review(BaseModel):
    photos: list[PhotoReview]
    missing: list[str] = Field(description="required steps without a photo")
    ready: bool = Field(description="every required step has a usable photo")


class EnrollResult(BaseModel):
    employee_code: str
    saved: list[str] = Field(description="photo files written to known/<code>/")
    accepted: int = Field(description="photos in the person's folder that passed the checks")
    embeddings: int


def step_of(key: str) -> enroll_guide.Step:
    for s in enroll_guide.steps():
        if s.key == key:
            return s
    raise HTTPException(422, f"Unknown step {key!r}; use one of: {', '.join(s.key for s in enroll_guide.steps())}.")


def decode(data: bytes):
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "The image is too large.")
    img = enrollment.decode_image(data)
    if img is None:
        raise HTTPException(422, "That isn't a readable JPEG or PNG image.")
    return img


def decode_b64(text: str):
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return decode(base64.b64decode(text, validate=True))
    except (binascii.Error, ValueError):
        raise HTTPException(422, "The photo isn't valid base64.") from None


class EnrollmentSource(BaseModel):
    key: str = Field(description='"webcam" (this PC\'s webcam, in the browser) or "camera:<id>"')
    kind: Literal["webcam", "camera"]
    label: str
    camera_id: int | None = None
    direction: str | None = None
    site: str | None = None
    running: bool = Field(False, description="its worker runs: the wizard shares its frames, attendance goes on")


class EnrollmentSources(BaseModel):
    sources: list[EnrollmentSource]
    last: str | None = Field(description="this user's last choice, if it still exists")


class SessionIn(BaseModel):
    camera_id: int
    session_id: str | None = Field(None, description="renew this session; omit (or a lapsed one) for a new one")


class SessionOut(BaseModel):
    session_id: str
    camera_id: int
    camera: str
    feed: Literal["camera", "worker"] = Field(description='"worker": the running worker\'s frames are shared')
    expires_seconds: float
    renew_seconds: float


class CameraCheck(BaseModel):
    """FrameCheck for an IP camera session, plus whether frames arrive and the window's size."""
    ok: bool
    state: str
    message: str
    yaw: float | None
    pitch: float | None
    face_px: int | None
    score: float | None
    box: list[float] | None
    guide: list[float]
    connected: bool = Field(description="frames are arriving")
    width: int | None = Field(description="the enrollment window at full resolution")
    height: int | None
    feed: Literal["camera", "worker"]


class Captured(BaseModel):
    step: str
    image: str = Field(description="the full-resolution enrollment window, JPEG as a data: URL")
    width: int
    height: int


SOURCE_PREF = "enroll_source"


class WebcamClaimIn(BaseModel):
    claim_id: str | None = Field(None, description="renew this claim; omit (or a lapsed one) for a new claim")


class WebcamClaim(BaseModel):
    claim_id: str
    expires_seconds: int = Field(description="the claim lapses unless renewed within this many seconds")
    renew_seconds: int = Field(description="renew it (POST again with claim_id) this often")


def fractions(box, w: int, h: int) -> list[float]:
    x1, y1, x2, y2 = (float(v) for v in box)
    return [round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)]


@router.post("/api/enrollment/webcam", response_model=WebcamClaim,
             responses={409: {"description": "a webcam camera worker is running"}})
def claim_webcam(request: Request, body: WebcamClaimIn | None = None,
                 user: CurrentUser = Depends(manage)) -> WebcamClaim:
    """Hold the webcam for the enrollment wizard (or renew the hold): webcam camera workers can't start meanwhile.
    409 while a webcam worker runs (a renewal of a live claim is never refused)."""
    try:
        claim_id = request.app.state.workers.claim_webcam(user.username, body.claim_id if body else None)
    except CameraBusy as e:
        raise HTTPException(409, f"{str(e)[:1].upper()}{str(e)[1:]}.") from None
    if not body or body.claim_id != claim_id:  # a new claim: remember the choice
        auth.db_of(request).set_preference(user.id, SOURCE_PREF, "webcam")
    return WebcamClaim(claim_id=claim_id, expires_seconds=config.ENROLL_CAMERA_CLAIM_SECONDS,
                       renew_seconds=config.ENROLL_CAMERA_RENEW_SECONDS)


@router.delete("/api/enrollment/webcam/{claim_id}", status_code=204)
def release_webcam(claim_id: str, request: Request, _: CurrentUser = Depends(manage)) -> None:
    """Release the webcam (the wizard closed). Releasing a claim that already lapsed is fine too."""
    request.app.state.workers.release_webcam(claim_id)


# --- IP cameras ---------------------------------------------------------------------------------------------


def sentence(e: Exception) -> str:
    text = str(e)
    return f"{text[:1].upper()}{text[1:]}{'' if text.endswith('.') else '.'}"


@router.get("/api/enrollment/sources", response_model=EnrollmentSources)
def enrollment_sources(request: Request, user: CurrentUser = Depends(manage)) -> EnrollmentSources:
    """The wizard's "Which camera?": this PC's webcam and every IP camera (ENROLL_IP_TYPES), with the user's last
    choice. No camera sources or passwords."""
    items = [EnrollmentSource(key="webcam", kind="webcam", label="This PC's webcam")]
    for c in camera_routes.cameras(request):
        if c["type"] in config.ENROLL_IP_TYPES:
            items.append(EnrollmentSource(key=f"camera:{c['id']}", kind="camera", label=c["name"], camera_id=c["id"],
                                          direction=c["direction"], site=c["site"],
                                          running=c["worker"].state != "stopped"))
    last = auth.db_of(request).preference(user.id, SOURCE_PREF)
    return EnrollmentSources(sources=items, last=last if any(i.key == last for i in items) else None)


@router.post("/api/enrollment/sessions", response_model=SessionOut, responses={
    409: {"description": "another user's wizard has the camera, or its password variable is missing"}})
def open_session(body: SessionIn, request: Request, user: CurrentUser = Depends(manage)) -> SessionOut:
    """Hold an IP camera for the wizard (or renew the hold). A running worker's frames are shared (attendance goes
    on); otherwise the server opens the camera's main stream, and its worker can't start until the session ends.
    Renew every renew_seconds; a session not renewed for expires_seconds lapses."""
    cam = camera_routes.get_or_404(request, body.camera_id)
    if cam["type"] not in config.ENROLL_IP_TYPES:
        raise HTTPException(422, f"{cam['name']} isn't an IP camera: use this PC's webcam instead.")
    try:
        session, new = request.app.state.enroll_cameras.open(user.username, cam["id"],
                                                             camera_routes.entry_of(cam["name"]), body.session_id)
    except (CameraBusy, WorkerError) as e:
        raise HTTPException(409, sentence(e)) from None
    if new:
        auth.db_of(request).set_preference(user.id, SOURCE_PREF, f"camera:{cam['id']}")
    return SessionOut(session_id=session.id, camera_id=cam["id"], camera=cam["name"], feed=session.feed.mode,
                      expires_seconds=config.ENROLL_CAMERA_CLAIM_SECONDS,
                      renew_seconds=config.ENROLL_CAMERA_RENEW_SECONDS)


@router.delete("/api/enrollment/sessions/{session_id}", status_code=204)
def close_session(session_id: str, request: Request, user: CurrentUser = Depends(manage)) -> None:
    """Release the camera (the wizard closed). Closing a session that already lapsed is fine too."""
    request.app.state.enroll_cameras.release(user.username, session_id)


def session_of(request: Request, user: CurrentUser, session_id: str):
    session = request.app.state.enroll_cameras.get(user.username, session_id)
    if session is None:
        raise HTTPException(404, "This camera session ended: open the camera again.")
    return session


def frame_check(img, result) -> dict:
    """The FrameCheck fields for one evaluated image (boxes as fractions of it)."""
    h, w = img.shape[:2]
    q = result.quality
    return dict(
        ok=result.state == "good", state=result.state, message=result.hint,
        yaw=None if result.yaw is None else round(result.yaw, 1),
        pitch=None if result.pitch is None else round(result.pitch, 1),
        face_px=q.face_px if q and q.detection is not None else None, score=q.score if q else None,
        box=fractions(result.face_box, w, h) if result.face_box is not None else None,
        guide=fractions(enroll_guide.guide_box(img.shape), w, h))


async def evaluate_window(request: Request, session, step):
    """(window, result) for the session's newest frame, or None when no frame has arrived yet."""
    got = session.latest()
    if got is None:
        return None
    img = window(got[1])
    detector, _recognizer = await run_in_threadpool(request.app.state.face_models)

    def run():
        with _enroll_lock:
            return enroll_guide.evaluate(img, step, detector)

    return img, await run_in_threadpool(run)


@router.post("/api/enrollment/sessions/{session_id}/check", response_model=CameraCheck)
async def check_session(session_id: str, request: Request, step: str = Query("front"),
                        user: CurrentUser = Depends(manage)) -> CameraCheck:
    """The same check as /api/enrollment/check, on the newest frame's enrollment window at full resolution."""
    s = step_of(step)
    session = await run_in_threadpool(session_of, request, user, session_id)
    done = await evaluate_window(request, session, s)
    if done is None:
        error = session.feed.error()
        return CameraCheck(ok=False, state="none", connected=False, feed=session.feed.mode, yaw=None, pitch=None,
                           face_px=None, score=None, box=None, width=None, height=None,
                           message=f"Waiting for the camera: {error}" if error else "Connecting to the camera…",
                           guide=fractions(enroll_guide.guide_box((9, 16, 3)), 16, 9))
    img, result = done
    return CameraCheck(**frame_check(img, result), connected=True, width=img.shape[1], height=img.shape[0],
                       feed=session.feed.mode)


@router.post("/api/enrollment/sessions/{session_id}/capture", response_model=Captured,
             responses={409: {"description": "the newest frame doesn't pass the step's checks"}})
async def capture_session(session_id: str, request: Request, step: str = Query("front"),
                          user: CurrentUser = Depends(manage)) -> Captured:
    """Take the photo for a step: the newest frame's enrollment window at full resolution (not the preview), if it
    passes the step's checks (409 with the reason otherwise). Nothing is saved yet: it goes to review and save."""
    s = step_of(step)
    session = await run_in_threadpool(session_of, request, user, session_id)
    done = await evaluate_window(request, session, s)
    if done is None:
        raise HTTPException(409, "No picture from the camera yet.")
    img, result = done
    if result.state != "good":
        raise HTTPException(409, f"{result.hint}.")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, config.ENROLL_CAPTURE_QUALITY])
    if not ok:
        raise HTTPException(500, "The photo couldn't be encoded.")
    return Captured(step=s.key, image="data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii"),
                    width=img.shape[1], height=img.shape[0])


@router.get("/api/enrollment/sessions/{session_id}/stream", response_class=StreamingResponse, responses={
    200: {"content": {f"multipart/x-mixed-replace; boundary={BOUNDARY}": {}},
          "description": "MJPEG: the enrollment window, up to ENROLL_PREVIEW_FPS a second and ENROLL_PREVIEW_WIDTH "
                         "wide; use it as an <img> src"}})
async def stream_session(session_id: str, request: Request,
                         max_frames: int | None = Query(None, ge=1, description="end after this many frames"),
                         user: CurrentUser = Depends(manage)) -> StreamingResponse:
    """The wizard's live preview of an IP camera session (the checks and the photo use the full resolution). Ends
    when the session ends."""
    session = await run_in_threadpool(session_of, request, user, session_id)

    def encode(frame) -> bytes:
        img = window(frame)
        if img.shape[1] > config.ENROLL_PREVIEW_WIDTH:
            k = config.ENROLL_PREVIEW_WIDTH / img.shape[1]
            img = cv2.resize(img, (config.ENROLL_PREVIEW_WIDTH, max(1, round(img.shape[0] * k))),
                             interpolation=cv2.INTER_AREA)
        return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, config.LIVE_JPEG_QUALITY])[1].tobytes()

    async def frames():
        sent, count, next_due = 0, 0, 0.0
        while not session.closed:
            now = time.monotonic()
            got = session.latest()
            if got and got[0] != sent and now >= next_due:
                sent, next_due = got[0], now + 1.0 / config.ENROLL_PREVIEW_FPS
                yield part(await run_in_threadpool(encode, got[1]))
                count += 1
                if max_frames and count >= max_frames:
                    return
            await asyncio.sleep(1.0 / (4 * config.ENROLL_PREVIEW_FPS))

    return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/enrollment/steps", response_model=list[StepInfo])
def enrollment_steps(_: CurrentUser = Depends(manage)) -> list[StepInfo]:
    """The guided steps in order (app/config.py ENROLL_STEPS)."""
    return [StepInfo(key=s.key, title=s.title, instruction=s.instruction, yaw=s.yaw, pitch=s.pitch, required=s.required)
            for s in enroll_guide.steps()]


@router.post("/api/enrollment/check", response_model=FrameCheck, openapi_extra={"requestBody": {
    "required": True, "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}})
async def check_frame(request: Request, step: str = Query("front", description="the step being captured"),
                      _: CurrentUser = Depends(manage)) -> FrameCheck:
    """Check one camera frame (the raw JPEG as the request body) against the enrollment rules and the step's
    angles. The face models load on first use, so the first call takes a few seconds."""
    s = step_of(step)
    img = decode(await request.body())
    detector, _recognizer = await run_in_threadpool(request.app.state.face_models)

    def run():
        with _enroll_lock:
            return enroll_guide.evaluate(img, s, detector)

    return FrameCheck(**frame_check(img, await run_in_threadpool(run)))


def captures(request: Request, photos: list[PhotoIn], code: str):
    """(session with the usable photos, per-photo review rows) — final_check per step, like the dashboard."""
    detector, recognizer = request.app.state.face_models()
    session = enroll_guide.GuidedSession(code)
    rows = []
    with _enroll_lock:
        for photo in photos:
            s = step_of(photo.step)
            cap, problem = enroll_guide.capture_photo(decode_b64(photo.image), s, detector, recognizer)
            if cap is not None:
                session.accept(cap)
            q = cap.quality if cap else None
            rows.append({"step": s.key, "ok": cap is not None, "problem": problem, "warnings": [],
                         "yaw": None if q is None else round(enroll_guide.person_yaw(q.yaw), 1),
                         "pitch": None if q is None else round(q.pitch, 1), "score": None if q is None else q.score})
    return session, rows


def review_of(session, rows) -> Review:
    warnings = enroll_guide.review(session, enrollment.load_gallery())
    for row in rows:
        row["warnings"] = warnings.get(row["step"], []) if row["ok"] else []
    missing = [s.key for s in session.required if s.key not in session.captures]
    return Review(photos=[PhotoReview(**r) for r in rows], missing=missing, ready=not missing)


@router.post("/api/enrollment/review", response_model=Review)
def review_photos(body: PhotosIn, request: Request, person_id: int | None = Query(None, description="who they are "
                  "(so their own photos aren't reported as \"looks like someone else\")"),
                  _: CurrentUser = Depends(manage)) -> Review:
    """Check the captured photos together before saving: each must pass the step's checks; warnings when one
    doesn't match the others or looks like another enrolled person. Nothing is saved."""
    code = get_or_404(request, person_id)["employee_code"] if person_id else ""
    return review_of(*captures(request, body.photos, code))


@router.post("/api/people/{person_id}/enroll", response_model=EnrollResult, tags=["people"])
def enroll_person(person_id: int, body: PhotosSave, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Save the guided photos to known/<code>/ (with their step) and rebuild the person's embeddings; running cameras
    pick them up within seconds. Every required step needs a usable photo (422 otherwise)."""
    person = get_or_404(request, person_id)
    session, rows = captures(request, body.photos, person["employee_code"])
    review = review_of(session, rows)
    problems = [f"{r.step}: {r.problem}" for r in review.photos if not r.ok]
    if problems or not review.ready:
        raise HTTPException(422, "; ".join(problems + [f"no photo for {m}" for m in review.missing]))
    detector, recognizer = request.app.state.face_models()
    before = {p.name for p in enrollment.photo_paths(enrollment.person_dir(person["employee_code"]))}
    db = auth.db_of(request)
    with _enroll_lock, db.conn() as conn:
        result = enroll_guide.finish(session, detector, recognizer, conn)
    saved = sorted({p.name for p in enrollment.photo_paths(enrollment.person_dir(person["employee_code"]))} - before)
    db.log_change(user.username, "enroll_person", f"person:{person['employee_code']}",
                  {"steps": [r.step for r in review.photos], "saved": saved, "source": body.source or "webcam",
                   "warnings": sum(len(r.warnings) for r in review.photos)}, body.reason_or("guided enrollment"))
    return {"employee_code": person["employee_code"], "saved": saved, "accepted": len(result.accepted),
            "embeddings": len(result.embeddings)}



