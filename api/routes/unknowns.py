"""Unknown visitors (app/visitors.py): the faces a camera saw that matched no enrolled person, kept for review while
the visitor log is on (Settings > System > Unknown visitors, /api/settings/unknowns).

Admins and guards see them (unknowns:read) and review them (unknowns:manage): dismiss, identify as a person (an
employee the cameras missed, or someone enrolled since), restore to new, add a note. Every review needs a reason and
goes to the audit log (review_unknown). Records and their snapshots are deleted after UNKNOWN_RETENTION_DAYS by the
nightly job (api/maintenance.py), whatever their status. Attendance never changes here: a visit is not an event.
"""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, list_params
from api.schemas import Reason, UnknownVisit, VisitStatus, day_range, visit_out
from app import config

router = APIRouter(prefix="/api/unknowns", tags=["unknowns"], responses=auth.PROTECTED_RESPONSES)
read = require_permission("unknowns:read")
manage = require_permission("unknowns:manage")


class UnknownCounts(BaseModel):
    enabled: bool = Field(description="the visitor log is on (Settings > System)")
    new: int = Field(description="visits not reviewed yet (status new)")
    today: int = Field(description="visits first seen today")


class VisitReview(Reason):
    status: VisitStatus = Field(description="new (not reviewed), dismissed, or identified (needs person_id)")
    person_id: int | None = Field(None, description="for identified: who it was")
    note: str | None = Field(None, max_length=2000, description="leave out to keep the note; empty to clear it")


def get_or_404(request: Request, visit_id: int) -> dict:
    row = auth.db_of(request).unknown_visit(visit_id)
    if row is None:
        raise HTTPException(404, f"No unknown visitor with id {visit_id}.")
    return row


@router.get("", response_model=Page[UnknownVisit])
def list_unknowns(request: Request, params: ListParams = Depends(list_params),
                  date_from: date | None = Query(None, alias="from", description="first day (default today)"),
                  date_to: date | None = Query(None, alias="to", description="last day, inclusive"),
                  camera: int | None = Query(None, description="camera id"),
                  status: VisitStatus | None = None, _: CurrentUser = Depends(read)) -> dict:
    """Unknown visitors first seen on these days, newest first by default. q searches the camera, the closest
    person, the identified person and the note; site_id = the camera's site. Sort: first_seen, last_seen, camera,
    score, tracks, status."""
    try:
        first, last = day_range(date_from, date_to)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    rows, total = auth.db_of(request).unknown_visits(
        params, datetime.combine(first, datetime.min.time()),
        datetime.combine(last + timedelta(days=1), datetime.min.time()), camera, status)
    return {"items": [visit_out(r) for r in rows], "total": total, "page": params.page,
            "page_size": params.page_size}


@router.get("/counts", response_model=UnknownCounts)
def unknown_counts(request: Request, _: CurrentUser = Depends(read)) -> UnknownCounts:
    """For the menu's badge: visits waiting for review, and today's."""
    return UnknownCounts(**auth.db_of(request).unknown_counts(date.today()))


@router.get("/{visit_id}", response_model=UnknownVisit)
def get_unknown(visit_id: int, request: Request, _: CurrentUser = Depends(read)) -> UnknownVisit:
    return visit_out(get_or_404(request, visit_id))


@router.get("/{visit_id}/snapshot", response_class=FileResponse,
            responses={200: {"content": {"image/jpeg": {}}, "description": "the visitor's snapshot (the face with "
                                                                           "some background)"},
                       404: {"description": "no such visit, or it has no snapshot"}})
def unknown_snapshot(visit_id: int, request: Request, _: CurrentUser = Depends(read)) -> FileResponse:
    """The visit's snapshot, for logged-in admins and guards only (never a public URL)."""
    row = get_or_404(request, visit_id)
    if not row["snapshot_path"]:
        raise HTTPException(404, "No snapshot for this visit.")
    base = (config.SNAPSHOT_DIR / config.UNKNOWN_SNAPSHOT_FOLDER).resolve()
    path = (config.DATA_DIR / row["snapshot_path"]).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(404, "The snapshot file is missing.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.patch("/{visit_id}", response_model=UnknownVisit)
def review_unknown(visit_id: int, body: VisitReview, request: Request,
                   user: CurrentUser = Depends(manage)) -> UnknownVisit:
    """Review a visit: dismiss it, identify it as a person, put it back to new, or change its note. Audited
    (review_unknown, the old and new values) with the reason; nothing else changes (attendance stays as it is)."""
    db = auth.db_of(request)
    get_or_404(request, visit_id)
    if body.status == "identified":
        if body.person_id is None:
            raise HTTPException(422, "Identified needs person_id: who it was.")
        if db.person(body.person_id) is None:
            raise HTTPException(404, f"No person with id {body.person_id}.")
    elif body.person_id is not None:
        raise HTTPException(422, "person_id goes with status identified only.")
    try:
        db.review_unknown(visit_id, body.status, body.person_id, body.note, user.username, body.reason)
    except LookupError as e:  # deleted meanwhile (the retention, or the camera withdrew it)
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return visit_out(get_or_404(request, visit_id))
