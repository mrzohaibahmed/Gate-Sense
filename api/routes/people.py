"""People: list, profile (photos, recent attendance), add, edit, deactivate / activate, re-enroll.

Uses the same functions as the dashboard's People and Enrollment pages (app.db, app.enrollment).
Every change is audited and needs a reason, except adding a person and their photos (enrollment), which get a default.
"""

import threading
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, list_params, paginate
from api.schemas import AttendanceRow, Event, OptionalReason, Reason, attendance_rows, event_out
from app import enrollment

router = APIRouter(prefix="/api/people", tags=["people"], responses=auth.PROTECTED_RESPONSES)
read = require_permission("people:read")
manage = require_permission("people:manage")
RECENT_DAYS = 14
_enroll_lock = threading.Lock()  # one re-enrollment at a time (the models aren't shared across threads)


class Person(BaseModel):
    id: int
    employee_code: str
    name: str
    department: str | None
    shift: str | None
    active: bool
    site_id: int | None
    site: str | None
    photos: int = Field(description="photos in known/<employee code>/")
    photo_url: str | None = Field(None, description="the first photo, for a thumbnail (logged-in users only)")
    embeddings: int = Field(description="photos in the recognition gallery (accepted at the last enrollment)")


class Photo(BaseModel):
    file: str
    url: str
    accepted: bool | None = Field(description="passed the enrollment checks (None: not checked yet)")
    message: str | None = Field(description="why it was refused, in plain words")
    score: float | None
    yaw: float | None
    pitch: float | None
    step: str | None = Field(description="guided-capture step (front, left, right), if taken that way")


class PersonDetail(Person):
    photo_list: list[Photo]
    recent_attendance: list[AttendanceRow] = Field(description=f"the last {RECENT_DAYS} days, newest first")
    recent_events: list[Event] = Field(description=f"events of the last {RECENT_DAYS} days, newest first")


class PersonIn(OptionalReason):
    employee_code: str = Field(description="also the photo folder name: letters, digits, '_', '-' or '.'")
    name: str = Field(min_length=1)
    department: str | None = None
    shift: str | None = None
    site_id: int | None = Field(None, description="default: the first site")


class PersonPatch(Reason):
    name: str | None = Field(None, min_length=1)
    department: str | None = None
    shift: str | None = None
    site_id: int | None = None


class ReenrollResult(BaseModel):
    employee_code: str
    accepted: list[str]
    rejected: list[dict[str, str]] = Field(description='[{"file": ..., "reason": ...}]')
    embeddings: int


def photo_files(code: str) -> list[str]:
    try:
        return [p.name for p in enrollment.photo_paths(enrollment.person_dir(code))]
    except ValueError:  # a code that isn't a valid folder name has no photos
        return []


def rows(request: Request) -> list[dict]:
    gallery = enrollment.load_gallery().counts()
    out = []
    for p in auth.db_of(request).people():
        files = photo_files(p["employee_code"])
        out.append({**p, "active": bool(p["active"]), "photos": len(files), "embeddings": gallery.get(p["employee_code"], 0),
                    "photo_url": f"/api/people/{p['id']}/photos/{files[0]}" if files else None})
    return out


def get_or_404(request: Request, person_id: int) -> dict:
    person = next((p for p in rows(request) if p["id"] == person_id), None)
    if person is None:
        raise HTTPException(404, f"No person with id {person_id}.")
    return person


def check_site(request: Request, site_id: int | None) -> None:
    if site_id is not None and auth.db_of(request).site(site_id) is None:
        raise HTTPException(422, f"No site with id {site_id}.")


@router.get("", response_model=Page[Person])
def list_people(request: Request, params: ListParams = Depends(list_params),
                active: bool | None = Query(None, description="only active (true) or inactive (false) people"),
                _: CurrentUser = Depends(read)) -> dict:
    items = [p for p in rows(request) if (params.site_id is None or p["site_id"] == params.site_id)
             and (active is None or p["active"] == active)]
    return paginate(items, params, ("name", "employee_code", "department", "shift"),
                    ("name", "employee_code", "department", "shift", "active", "site", "photos", "embeddings"), "name")


@router.get("/{person_id}", response_model=PersonDetail)
def get_person(person_id: int, request: Request, _: CurrentUser = Depends(read)) -> PersonDetail:
    person = get_or_404(request, person_id)
    db = auth.db_of(request)
    folder = enrollment.person_dir(person["employee_code"]) if person["photos"] else None
    stored = enrollment.read_sidecar(folder) if folder else {}
    photos = []
    for path in enrollment.photo_paths(folder) if folder else []:
        s = stored.get(path.name)
        photos.append(Photo(file=path.name, url=f"/api/people/{person_id}/photos/{path.name}",
                            accepted=s.get("ok") if s else None, message=(s.get("reason") or None) if s else None,
                            score=s.get("score") if s else None, yaw=s.get("yaw") if s else None,
                            pitch=s.get("pitch") if s else None, step=s.get("step") if s else None))
    today = date.today()
    first = today - timedelta(days=RECENT_DAYS - 1)
    days = [r for r in attendance_rows(db, first, today) if r["person_id"] == person_id]
    return PersonDetail(**person, photo_list=photos,
                        recent_attendance=sorted(days, key=lambda r: r["date"], reverse=True),
                        recent_events=[event_out(e) for e in db.recent_events(person_id, first)])


@router.get("/{person_id}/photos/{file}", response_class=FileResponse,
            responses={200: {"content": {"image/jpeg": {}}, "description": "an enrollment photo"}})
def person_photo(person_id: int, file: str, request: Request, _: CurrentUser = Depends(read)) -> FileResponse:
    person = get_or_404(request, person_id)
    try:
        paths = enrollment.photo_paths(enrollment.person_dir(person["employee_code"]))
    except ValueError:
        paths = []
    match = next((p for p in paths if p.name == file), None)  # only real photo names, never a path
    if match is None:
        raise HTTPException(404, "No such photo.")
    return FileResponse(match, headers={"Cache-Control": "private, max-age=300"})


@router.post("", response_model=Person, status_code=201)
def create_person(body: PersonIn, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Add a person (no photos yet: add them on the dashboard's Enrollment page, then re-enroll)."""
    try:
        code = enrollment.validate_code(body.employee_code)
    except ValueError as e:
        raise HTTPException(422, str(e).capitalize()) from None
    check_site(request, body.site_id)
    try:
        person_id = auth.db_of(request).add_person(code, body.name, body.department, body.shift, body.site_id,
                                                   user.username, body.reason_or("added for enrollment"))
    except ValueError as e:
        raise HTTPException(409 if "already exists" in str(e) else 422, str(e).capitalize()) from None
    return get_or_404(request, person_id)


@router.patch("/{person_id}", response_model=Person)
def update_person(person_id: int, body: PersonPatch, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Change name, department, shift or site (fields left out stay as they are)."""
    person = get_or_404(request, person_id)
    check_site(request, body.site_id)
    sent = body.model_fields_set
    auth.db_of(request).update_person(
        person_id, body.name if body.name is not None else person["name"],
        body.department if "department" in sent else person["department"],
        body.shift if "shift" in sent else person["shift"], body.site_id, user.username, body.reason)
    return get_or_404(request, person_id)


@router.post("/{person_id}/deactivate", response_model=Person)
def deactivate_person(person_id: int, body: Reason, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Inactive people are no longer recorded by the cameras; their history stays."""
    get_or_404(request, person_id)
    auth.db_of(request).set_person_active(person_id, False, user.username, body.reason)
    return get_or_404(request, person_id)


@router.post("/{person_id}/activate", response_model=Person)
def activate_person(person_id: int, body: Reason, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    get_or_404(request, person_id)
    auth.db_of(request).set_person_active(person_id, True, user.username, body.reason)
    return get_or_404(request, person_id)


@router.post("/{person_id}/reenroll", response_model=ReenrollResult)
def reenroll_person(person_id: int, body: Reason, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Check every photo in known/<employee code>/ again and rebuild the person's embeddings (running cameras pick
    them up within seconds). Loads the face models on first use, so the first call takes a few seconds."""
    person = get_or_404(request, person_id)
    if not person["photos"]:
        raise HTTPException(409, "This person has no photos: add them on the dashboard's Enrollment page first.")
    code = person["employee_code"]
    detector, recognizer = request.app.state.face_models()
    db = auth.db_of(request)
    with _enroll_lock, db.conn() as conn:
        result = enrollment.enroll_one(code, detector, recognizer, conn)
    details = {"accepted": len(result.accepted), "rejected": len(result.rejected)}
    db.log_change(user.username, "reenroll_person", f"person:{code}", details, body.reason)
    return {"employee_code": code, "accepted": result.accepted,
            "rejected": [{"file": f, "reason": r} for f, r in result.rejected], "embeddings": len(result.embeddings)}


# --- Photos and face data (upload photo files, delete one photo, delete all face data) --------------------------

MAX_UPLOAD_PHOTOS = 10


class UploadPhoto(BaseModel):
    name: str = Field(description="the file name, to report back")
    image: str = Field(description="JPEG or PNG, base64 (a data: URL prefix is allowed)")


class PhotosUpload(OptionalReason):
    photos: list[UploadPhoto] = Field(min_length=1, max_length=MAX_UPLOAD_PHOTOS)


class PhotoResult(BaseModel):
    name: str
    ok: bool = Field(description="passed the enrollment checks and was saved")
    message: str = Field(description="why it was refused, in plain words (empty when saved)")
    score: float | None
    yaw: float | None
    pitch: float | None
    saved_as: str | None = Field(description="the file in known/<employee code>/")


class UploadResult(BaseModel):
    results: list[PhotoResult]
    saved: int
    embeddings: int = Field(description="the person's embeddings after the rebuild")


class FaceDataDelete(Reason):
    deactivate: bool = Field(False, description="also deactivate the person (they stop showing as absent)")


class FaceDataDeleted(BaseModel):
    photos: int
    embeddings: int
    deactivated: bool


def decode_upload(text: str):
    """The image, or None when it can't be read (reported as a failed photo, not an error)."""
    import base64
    import binascii

    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return enrollment.decode_image(data) if data else None


@router.post("/{person_id}/photos", response_model=UploadResult)
def upload_photos(person_id: int, body: PhotosUpload, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Add photos from files (no camera): each goes through the same enrollment checks as the guided capture; the
    passing ones are saved to known/<employee code>/ and the person's embeddings rebuilt once. Audited."""
    person = get_or_404(request, person_id)
    code = person["employee_code"]
    detector, recognizer = request.app.state.face_models()
    db = auth.db_of(request)
    images = [(p.name, decode_upload(p.image)) for p in body.photos]
    with _enroll_lock, db.conn() as conn:
        checked, rebuilt = enrollment.add_photos(code, images, detector, recognizer, conn)
    results = []
    for name, check, path in checked:
        q = check.quality
        results.append({"name": name, "ok": path is not None, "message": "" if path else check.message,
                        "score": q.score if q and path else None, "yaw": q.yaw if q else None,
                        "pitch": q.pitch if q else None, "saved_as": path.name if path else None})
    saved = [r["saved_as"] for r in results if r["saved_as"]]
    db.log_change(user.username, "add_photos", f"person:{code}",
                  {"saved": saved, "refused": [r["name"] for r in results if not r["ok"]]},
                  body.reason_or("enrollment photos from files"))
    embeddings = len(rebuilt.embeddings) if rebuilt else enrollment.load_gallery().counts().get(code, 0)
    return {"results": results, "saved": len(saved), "embeddings": embeddings}


@router.delete("/{person_id}/photos/{file}", response_model=Person)
def delete_person_photo(person_id: int, file: str, request: Request,
                        reason: str = Query(..., min_length=1, description="why (goes to the audit log)"),
                        user: CurrentUser = Depends(manage)) -> dict:
    """Delete one photo and rebuild the person's embeddings. Audited."""
    person = get_or_404(request, person_id)
    if not reason.strip():
        raise HTTPException(422, "A reason is required.")
    code = person["employee_code"]
    detector, recognizer = request.app.state.face_models()
    db = auth.db_of(request)
    try:
        with _enroll_lock, db.conn() as conn:
            result = enrollment.delete_photo(code, file, detector, recognizer, conn)
    except ValueError:
        raise HTTPException(404, "No such photo.") from None
    db.log_change(user.username, "delete_photo", f"person:{code}",
                  {"photo": file, "embeddings": len(result.embeddings)}, reason.strip())
    return get_or_404(request, person_id)


@router.post("/{person_id}/delete-face-data", response_model=FaceDataDeleted)
def delete_face_data(person_id: int, body: FaceDataDelete, request: Request,
                     user: CurrentUser = Depends(manage)) -> dict:
    """Delete all the person's photos and face embeddings. The person and their attendance history stay (and they
    are no longer recognised until enrolled again). Audited."""
    person = get_or_404(request, person_id)
    db = auth.db_of(request)
    with _enroll_lock, db.conn() as conn:
        details = enrollment.delete_face_data(person["employee_code"], body.reason, user.username, conn,
                                              body.deactivate)
    return {"photos": details["photos"], "embeddings": details["embeddings"], "deactivated": body.deactivate}
