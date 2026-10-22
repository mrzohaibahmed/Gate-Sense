"""People: list, profile (photos, recent attendance), add, edit, deactivate / activate, re-enroll.

Uses the same functions as the dashboard's People and Enrollment pages (app.db, app.enrollment).
Every change needs a reason and is audited.
"""

import threading
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, list_params, paginate
from api.schemas import AttendanceRow, Event, Reason, attendance_rows, event_out
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


class PersonIn(Reason):
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


def counts(code: str, gallery: dict[str, int]) -> tuple[int, int]:
    try:
        photos = len(enrollment.photo_paths(enrollment.person_dir(code)))
    except ValueError:  # a code that isn't a valid folder name has no photos
        photos = 0
    return photos, gallery.get(code, 0)


def rows(request: Request) -> list[dict]:
    gallery = enrollment.load_gallery().counts()
    out = []
    for p in auth.db_of(request).people():
        photos, embeddings = counts(p["employee_code"], gallery)
        out.append({**p, "active": bool(p["active"]), "photos": photos, "embeddings": embeddings})
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
                                                   user.username, body.reason)
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
