"""Daily attendance rows (roster days, app/shifts.py), raw events with their snapshots, corrections, and the day
overview."""

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
from app.shifts import (
    ABSENT, CALENDAR_CAME_IN, CAME_IN, CHECK, COMPLETE, EARLY_LEAVE, LATE, MISSING_IN, MISSING_OUT, NOT_IN_YET, OFF_DAY,
    ON_TIME, UNSCHEDULED,
)

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
    """One row per roster day: every roster row (a shift or OFF) plus unscheduled dates with passes, computed from
    the events (voided ones ignored) with the shift's times in force that day. The first IN in the shift's IN window,
    the last OUT in its OUT window, worked time, late / early leave / overtime minutes and the status: on time, late,
    early leave, missing IN, missing OUT, absent, not in yet, check, off day, unscheduled. A night shift is dated the
    day it starts. Any other event is a repeat. site_id = the person's site. Default sort: -date, then name."""
    first, last = dates(date_from, date_to)
    rows = attendance_rows(auth.db_of(request), first, last)
    rows = [r for r in rows if (person is None or r["person_id"] == person) and (status is None or r["status"] == status)
            and (params.site_id is None or r["site_id"] == params.site_id)
            and (department is None or (r["department"] or "") == department)]
    return paginate(rows, params, ("name", "employee_code", "department", "shift"),
                    ("date", "name", "employee_code", "department", "shift", "first_in", "last_out", "worked_hours",
                     "late_minutes", "early_leave_minutes", "overtime_minutes", "status"),
                    "-date", tiebreak="name")


# --- Raw events ---------------------------------------------------------------------------------


@router.get("/api/events", response_model=Page[Event])
def list_events(request: Request, params: ListParams = Depends(list_params),
                date_from: date | None = Query(None, alias="from", description="first day (default today)"),
                date_to: date | None = Query(None, alias="to", description="last day, inclusive"),
                camera: int | None = Query(None, description="camera id"), direction: Direction | None = None,
                person: int | None = Query(None, description="person id"), _: CurrentUser = Depends(read)) -> dict:
    """Raw IN/OUT events with score and snapshot URL, manual and voided ones included; `repeat` marks the events no
    roster day uses (not the shift's first IN or last OUT). site_id = the camera's site. Default sort: -timestamp."""
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
                             "local times, and shifts are local times too")
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
    shifts: bool = Field(description="shifts on (roster days) or off (calendar days: complete, missing IN/OUT, check)")
    people: int = Field(description="roster days that date: every roster row (a shift or OFF), plus unscheduled "
                                    "people with passes")
    scheduled: int = Field(description="rostered on a shift (not OFF)")
    present: int = Field(description="came in for their shift (an IN in its IN window: on time, late, early leave, "
                                     "missing OUT)")
    inside_now: int | None = Field(description="people whose open day (a shift still running, from yesterday or "
                                               "today) ends with an IN: in the building now; only for today")
    complete: int = Field(description="shifts off: an IN and a later last OUT that date")
    on_time: int
    late: int
    early_leave: int
    overtime: int = Field(description="days with an OUT after the shift's end")
    missing_in: int = Field(description="an OUT for the shift but no IN in its IN window")
    missing_out: int = Field(description="an IN, no OUT after it, and the OUT window (shifts off: the date) is "
                                         "over")
    check: int = Field(description="passes in the shift's time, but none where they count: needs a look")
    absent: int = Field(description="rostered, the IN window has closed, no pass at all")
    not_in_yet: int = Field(description="rostered, no IN yet, the IN window still open")
    off_day: int = Field(description="OFF on the roster")
    unscheduled: int = Field(description="passes, but no roster row for that date")
    arrivals_by_hour: list[HourCount]
    cameras: list[CameraStatus]


@router.get("/api/overview", response_model=Overview)
def get_overview(request: Request, day: date | None = Query(None, alias="date", description="default today"),
                 site_id: int | None = Query(None, description="people and cameras of this site only"),
                 _: CurrentUser = Depends(read)) -> Overview:
    """The day at a glance: the roster days' statuses (like the Attendance page), arrivals per hour and every
    camera's status."""
    db = auth.db_of(request)
    day = day or date.today()
    today = day == date.today()
    both = [r for r in attendance_rows(db, day - timedelta(days=1) if today else day, day)
            if site_id is None or r["site_id"] == site_id]
    rows = [r for r in both if r["date"] == day]
    shifts = db.shift_mode()["enabled"]
    count = {s: sum(r["status"] == s for r in rows) for s in (
        ON_TIME, LATE, EARLY_LEAVE, MISSING_IN, CHECK, ABSENT, NOT_IN_YET, OFF_DAY, UNSCHEDULED, COMPLETE)}
    count[MISSING_OUT] = sum(r["status"] == MISSING_OUT and not r["open"] for r in rows)  # open: still inside
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
    inside = {r["person_id"] for r in both if r["open"] and r["inside"]}  # a night shift from yesterday counts
    return Overview(
        date=day, shifts=shifts, people=len(rows), scheduled=sum(r["shift"] is not None for r in rows),
        present=sum(r["status"] in (CAME_IN if shifts else CALENDAR_CAME_IN) for r in rows),
        inside_now=len(inside) if today else None, complete=count[COMPLETE], on_time=count[ON_TIME],
        late=count[LATE], early_leave=count[EARLY_LEAVE],
        overtime=sum((r["overtime_minutes"] or 0) > 0 for r in rows), missing_in=count[MISSING_IN],
        missing_out=count[MISSING_OUT], check=count[CHECK], absent=count[ABSENT], not_in_yet=count[NOT_IN_YET],
        off_day=count[OFF_DAY], unscheduled=count[UNSCHEDULED],
        arrivals_by_hour=[HourCount(hour=h, arrivals=n) for h, n in enumerate(hours)], cameras=cameras)
