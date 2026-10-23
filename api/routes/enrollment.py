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
"""

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import auth
from api.auth import CurrentUser, require_permission
from api.routes.people import _enroll_lock, get_or_404
from api.schemas import Reason
from api.workers import CameraBusy
from app import config, enroll_guide, enrollment

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


class PhotosSave(PhotosIn, Reason):
    pass


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
    return WebcamClaim(claim_id=claim_id, expires_seconds=config.ENROLL_CAMERA_CLAIM_SECONDS,
                       renew_seconds=config.ENROLL_CAMERA_RENEW_SECONDS)


@router.delete("/api/enrollment/webcam/{claim_id}", status_code=204)
def release_webcam(claim_id: str, request: Request, _: CurrentUser = Depends(manage)) -> None:
    """Release the webcam (the wizard closed). Releasing a claim that already lapsed is fine too."""
    request.app.state.workers.release_webcam(claim_id)


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

    result = await run_in_threadpool(run)
    h, w = img.shape[:2]
    q = result.quality
    return FrameCheck(
        ok=result.state == "good", state=result.state, message=result.hint,
        yaw=None if result.yaw is None else round(result.yaw, 1),
        pitch=None if result.pitch is None else round(result.pitch, 1),
        face_px=q.face_px if q and q.detection is not None else None, score=q.score if q else None,
        box=fractions(result.face_box, w, h) if result.face_box is not None else None,
        guide=fractions(enroll_guide.guide_box(img.shape), w, h))


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
                  {"steps": [r.step for r in review.photos], "saved": saved,
                   "warnings": sum(len(r.warnings) for r in review.photos)}, body.reason)
    return {"employee_code": person["employee_code"], "saved": saved, "accepted": len(result.accepted),
            "embeddings": len(result.embeddings)}



