"""Daily attendance rows, raw events with their snapshots, corrections, and the day overview."""

from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, list_params, paginate
from api.schemas import (
    AttendanceRow, Direction, Event, Reason, Status, attendance_rows, day_range, event_out,
)
from api.workers import WorkerInfo
from app import config
from app.attendance import ABSENT, CAME_IN, CHECK, COMPLETE, MISSING_ENTRY, MISSING_EXIT

router = APIRouter(tags=["attendance"], responses=auth.PROTECTED_RESPONSES)
read = require_permission("attendance:read")


def dates(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    try:
        return day_range(date_from, date_to)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


# --- Daily rows -------------------------------------------------------------------------------


@router.get("/api/attendance", response_model=Page[AttendanceRow])
def list_attendance(request: Request, params: ListParams = Depends(list_params),
                    date_from: date | None = Query(None, alias="from", description="first day (default today)"),
                    date_to: date | None = Query(None, alias="to", description="last day, inclusive"),
                    person: int | None = Query(None, description="person id"),
                    status: Status | None = None,
                    department: str | None = Query(None, description="exact department name"),
                    _: CurrentUser = Depends(read)) -> dict:
    """One row per person per day: first IN, last OUT, worked time and status, computed from the events (voided ones
    ignored; any other event is a repeat). Status: complete, missing exit (today: still inside), missing entry, check
    (the last OUT is earlier than the first IN; no hours), absent. site_id = the person's site. Default sort: -date,
    then name."""
    first, last = dates(date_from, date_to)
    rows = attendance_rows(auth.db_of(request), first, last)
    rows = [r for r in rows if (person is None or r["person_id"] == person) and (status is None or r["status"] == status)
            and (params.site_id is None or r["site_id"] == params.site_id)
            and (department is None or (r["department"] or "") == department)]
    return paginate(rows, params, ("name", "employee_code", "department"),
                    ("date", "name", "employee_code", "department", "first_in", "last_out", "worked_hours", "status"),
                    "-date", tiebreak="name")


# --- Raw events ---------------------------------------------------------------------------------


@router.get("/api/events", response_model=Page[Event])
def list_events(request: Request, params: ListParams = Depends(list_params),
                date_from: date | None = Query(None, alias="from", description="first day (default today)"),
                date_to: date | None = Query(None, alias="to", description="last day, inclusive"),
                camera: int | None = Query(None, description="camera id"), direction: Direction | None = None,
                person: int | None = Query(None, description="person id"), _: CurrentUser = Depends(read)) -> dict:
    """Raw IN/OUT events with score and snapshot URL, manual and voided ones included; `repeat` marks the events the
    person's day doesn't use (not the first IN or the last OUT). site_id = the camera's site. Default sort:
    -timestamp."""
    first, last = dates(date_from, date_to)
    db = auth.db_of(request)
    rows, total = db.events(params, datetime.combine(first, datetime.min.time()),
                            datetime.combine(last + timedelta(days=1), datetime.min.time()), camera, direction, person)
    repeats = db.repeat_ids(rows)
    return {"items": [event_out(r, repeats) for r in rows], "total": total, "page": params.page,
            "page_size": params.page_size}


@router.get("/api/snapshots/{event_id}", response_class=FileResponse,
            responses={200: {"content": {"image/jpeg": {}}, "description": "the face snapshot of the event"},
                       404: {"description": "no such event, or it has no snapshot"}})
def snapshot(event_id: int, request: Request, _: CurrentUser = Depends(read)) -> FileResponse:
    """The event's face snapshot, for logged-in users only (never a public URL)."""
    row = auth.db_of(request).event(event_id)
    if row is None or not row["snapshot_path"]:
        raise HTTPException(404, "No snapshot for this event.")
    base = config.DATA_DIR.resolve()
    path = (base / row["snapshot_path"]).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(404, "The snapshot file is missing.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# --- Corrections -----------------------------------------------------------------------------------


class Correction(Reason):
    action: Literal["add", "change", "void", "restore"] = Field(
        description="add: a manual event (person_id, timestamp, direction or camera_id); change: a new time for "
                    "event_id (the original time is kept); void / restore: event_id")
    event_id: int | None = None
    person_id: int | None = None
    direction: Direction | None = Field(None, description="for add: the first camera with this direction is used "
                                                          "(the person's site first) unless camera_id is given")
    camera_id: int | None = None
    timestamp: datetime | None = Field(None, description="local time without a time zone, e.g. 2026-10-20T08:30:00")

    @field_validator("timestamp")
    @classmethod
    def local_time(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is not None:
            raise ValueError("give the local time without a time zone (e.g. 2026-10-20T08:30:00): GateSense stores "
                             "local times, and the day runs from local midnight to midnight")
        return v


@router.post("/api/attendance/corrections", response_model=Event, status_code=201)
def add_correction(body: Correction, request: Request,
                   user: CurrentUser = Depends(require_permission("attendance:correct"))) -> Event:
    """Add an IN/OUT event, change an event's time, or void / restore one. Nothing is deleted; the original time
    is kept on the event; every correction needs a reason and goes to the audit log. HR and admin only."""
    db = auth.db_of(request)
    if body.timestamp and body.timestamp > datetime.now() + timedelta(minutes=1):
        raise HTTPException(422, "The time is in the future.")
    if body.action == "add":
        if body.person_id is None or body.timestamp is None or not (body.direction or body.camera_id):
            raise HTTPException(422, "add needs person_id, timestamp and a direction or camera_id.")
        person = db.person(body.person_id)
        if person is None:
            raise HTTPException(404, f"No person with id {body.person_id}.")
        camera_id = pick_camera(request, person, body.direction, body.camera_id)
        event_id = db.add_manual_event(person["id"], camera_id, body.timestamp, user.username, body.reason)
    else:
        if body.event_id is None or db.event(body.event_id) is None:
            raise HTTPException(404 if body.event_id else 422, "No such event." if body.event_id else
                                f"{body.action} needs event_id.")
        event_id = body.event_id
        if body.action == "change":
            if body.timestamp is None:
                raise HTTPException(422, "change needs the new timestamp.")
            db.edit_event_time(event_id, body.timestamp, user.username, body.reason)
        else:
            db.set_event_voided(event_id, body.action == "void", user.username, body.reason)
    row = db.event(event_id)
    return event_out(row, db.repeat_ids([row]))


def pick_camera(request: Request, person: dict, direction: str | None, camera_id: int | None) -> int:
    cams = auth.db_of(request).cameras(request.app.state.workers.cameras)
    if camera_id is not None:
        cam = next((c for c in cams if c["id"] == camera_id), None)
        if cam is None:
            raise HTTPException(404, f"No camera with id {camera_id}.")
        if direction and cam["direction"] != direction:
            raise HTTPException(422, f"Camera {cam['name']} records {cam['direction']}, not {direction}.")
        return cam["id"]
    configured = [c["name"] for c in request.app.state.workers.cameras]
    candidates = sorted((c for c in cams if c["direction"] == direction and c["name"] in configured),
                        key=lambda c: (c["site_id"] != person["site_id"], configured.index(c["name"])))
    if not candidates:
        raise HTTPException(409, f"No {direction} camera is configured.")
    return candidates[0]["id"]


# --- Overview ------------------------------------------------------------------------------------------


class HourCount(BaseModel):
    hour: int = Field(ge=0, le=23)
    arrivals: int = Field(description="people whose first IN was in this hour")


class CameraStatus(WorkerInfo):
    id: int
    direction: Direction
    site_id: int | None
    events: int = Field(description="events recorded by this camera on the day (not voided)")


class Overview(BaseModel):
    date: date
    people: int = Field(description="people counted: everyone active, plus anyone with events that day")
    present: int = Field(description="came in that day (has an IN: complete, missing exit or check)")
    inside_now: int | None = Field(description="their last event today is an IN (in the building now); only for "
                                               "today (None otherwise)")
    complete: int = Field(description="first IN and a later last OUT")
    absent: int
    missing_exit: int = Field(description="an IN but no OUT at all (for today: still inside, not a problem yet)")
    missing_entry: int = Field(description="went out without an IN that day")
    check: int = Field(description="the last OUT is earlier than the first IN: needs a look")
    arrivals_by_hour: list[HourCount]
    cameras: list[CameraStatus]


@router.get("/api/overview", response_model=Overview)
def get_overview(request: Request, day: date | None = Query(None, alias="date", description="default today"),
                 site_id: int | None = Query(None, description="people and cameras of this site only"),
                 _: CurrentUser = Depends(read)) -> Overview:
    """The day at a glance: counts like the Today page, arrivals per hour and every camera's status."""
    db = auth.db_of(request)
    day = day or date.today()
    rows = [r for r in attendance_rows(db, day, day) if site_id is None or r["site_id"] == site_id]
    count = {s: sum(r["status"] == s for r in rows) for s in (COMPLETE, MISSING_EXIT, MISSING_ENTRY, CHECK, ABSENT)}
    hours = [0] * 24
    for r in rows:
        if r["first_in"]:
            hours[r["first_in"].hour] += 1
    manager = request.app.state.workers
    table = {c["name"]: c for c in db.cameras(manager.cameras)}
    events = db.events_by_camera(day)
    cameras = []
    for info in manager.statuses():
        row = table[info.camera]
        if site_id is None or row["site_id"] == site_id:
            cameras.append(CameraStatus(**info.model_dump(), id=row["id"], direction=row["direction"],
                                        site_id=row["site_id"], events=events.get(row["id"], 0)))
    return Overview(
        date=day, people=len(rows), present=sum(r["status"] in CAME_IN for r in rows),
        inside_now=sum(r["inside"] for r in rows) if day == date.today() else None, complete=count[COMPLETE],
        absent=count[ABSENT], missing_exit=count[MISSING_EXIT], missing_entry=count[MISSING_ENTRY],
        check=count[CHECK],
        arrivals_by_hour=[HourCount(hour=h, arrivals=n) for h, n in enumerate(hours)], cameras=cameras)
