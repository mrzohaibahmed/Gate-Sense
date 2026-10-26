"""Shifts: the names and the effective-dated times the roster uses (app/shifts.py, app/db.py shift_versions).

A shift's times, grace and windows are versions: the first applies from the start, and every change applies from the
date it is given (default today) until the next one, so the days before a change keep the times they had. A change
that hasn't started yet can be withdrawn. The name is a label, changed at once (past days show the new name). Read by
everyone who can see the roster; changed by admin and HR (shifts:manage), each change with a reason, audited.
"""

from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, global_list_params, paginate
from api.schemas import Reason
from app import config
from app.shifts import Shift

router = APIRouter(prefix="/api/shifts", tags=["shifts"], dependencies=[Depends(auth.shifts_on)],
                   responses={**auth.PROTECTED_RESPONSES, 409: {"description": "shifts are switched off"}})
read = require_permission("roster:read")
manage = require_permission("shifts:manage")


class ShiftVersion(BaseModel):
    id: int
    effective_from: date | None = Field(description="the first day these times apply; None = from the start")
    start: str = Field(description="HH:MM")
    end: str = Field(description="HH:MM (the next day when crosses_midnight)")
    crosses_midnight: bool
    grace_min: int
    in_window_before_min: int
    out_window_after_min: int
    created: datetime
    created_by: str
    planned: bool = Field(description="starts after today: not in force yet, can be withdrawn")


class ShiftOut(BaseModel):
    id: int
    name: str
    start: str = Field(description="HH:MM, the times in force on the asked date (default today)")
    end: str
    crosses_midnight: bool
    grace_min: int = Field(description="IN up to start + grace is on time; OUT before end - grace is an early leave")
    in_window_before_min: int = Field(description="the IN window opens this long before the start")
    out_window_after_min: int = Field(description="the OUT window closes this long after the end")
    effective_from: date | None = Field(description="when the times in force began; None = from the start")
    versions: list[ShiftVersion] = Field(description="every version, oldest first (planned ones included)")
    roster_days: int = Field(description="roster entries using this shift (it can't be deleted while any do)")


class ShiftIn(Reason):
    name: str = Field(min_length=1, max_length=40)
    start: time = Field(description="local time, e.g. 06:30")
    end: time = Field(description="local time; at or before start = ends the next day")
    crosses_midnight: bool | None = Field(None, description="left out: worked out from start and end")
    grace_min: int = Field(config.SHIFT_GRACE_MIN, ge=0, le=config.SHIFT_MINUTES_MAX)
    in_window_before_min: int = Field(config.SHIFT_IN_WINDOW_BEFORE_MIN, ge=0, le=config.SHIFT_MINUTES_MAX)
    out_window_after_min: int = Field(config.SHIFT_OUT_WINDOW_AFTER_MIN, ge=0, le=config.SHIFT_MINUTES_MAX)


class ShiftPatch(Reason):
    name: str | None = Field(None, min_length=1, max_length=40, description="a new name (applies everywhere)")
    start: time | None = None
    end: time | None = None
    crosses_midnight: bool | None = None
    grace_min: int | None = Field(None, ge=0, le=config.SHIFT_MINUTES_MAX)
    in_window_before_min: int | None = Field(None, ge=0, le=config.SHIFT_MINUTES_MAX)
    out_window_after_min: int | None = Field(None, ge=0, le=config.SHIFT_MINUTES_MAX)
    effective_from: date | None = Field(None, description="the first day the new times apply (default today); the "
                                                          "days before keep the old ones")


def make_shift(name: str, start: time, end: time, crosses: bool | None, grace: int, before: int, after: int) -> Shift:
    try:
        return Shift(name, start.replace(tzinfo=None), end.replace(tzinfo=None),
                     end <= start if crosses is None else crosses, grace, before, after)
    except ValueError as e:
        raise HTTPException(422, str(e)[0].upper() + str(e)[1:] + ".") from None


def shift_out(item: dict) -> ShiftOut:
    s, today = item["shift"], date.today()
    versions = [ShiftVersion(
        id=v["id"], effective_from=v["effective_from"], start=v["start_time"], end=v["end_time"],
        crosses_midnight=bool(v["crosses_midnight"]), grace_min=v["grace_min"],
        in_window_before_min=v["in_window_before_min"], out_window_after_min=v["out_window_after_min"],
        created=v["created"], created_by=v["created_by"],
        planned=bool(v["effective_from"]) and date.fromisoformat(v["effective_from"]) > today)
        for v in item["versions"]]
    return ShiftOut(id=s.id, name=s.name, start=f"{s.start:%H:%M}", end=f"{s.end:%H:%M}",
                    crosses_midnight=s.crosses_midnight, grace_min=s.grace_min,
                    in_window_before_min=s.in_window_before_min, out_window_after_min=s.out_window_after_min,
                    effective_from=s.effective_from, versions=versions, roster_days=item["roster_days"])


def get_or_404(request: Request, shift_id: int, on: date | None = None) -> dict:
    item = auth.db_of(request).shift(shift_id, on)
    if item is None:
        raise HTTPException(404, f"No shift with id {shift_id}.")
    return item


def name_free(request: Request, name: str, except_id: int | None = None) -> None:
    if any(s["shift"].name.lower() == name.lower() and s["shift"].id != except_id
           for s in auth.db_of(request).shifts()):
        raise HTTPException(409, f"There is already a shift called {name!r}.")


@router.get("", response_model=Page[ShiftOut])
def list_shifts(request: Request, params: ListParams = Depends(global_list_params),
                on: date | None = Query(None, description="the times in force on this date (default today)"),
                _: CurrentUser = Depends(read)) -> dict:
    """Every shift with the times in force on `on`, its versions and how many roster days use it. Default sort:
    start time."""
    outs = [shift_out(s) for s in auth.db_of(request).shifts(on)]
    rows = [{**o.model_dump(), "_out": o} for o in outs]
    page = paginate(rows, params, ("name",), ("name", "start", "id"), "start", tiebreak="name")
    page["items"] = [r["_out"] for r in page["items"]]
    return page


@router.get("/{shift_id}", response_model=ShiftOut)
def get_shift(shift_id: int, request: Request, on: date | None = Query(None, description="default today"),
              _: CurrentUser = Depends(read)) -> ShiftOut:
    return shift_out(get_or_404(request, shift_id, on))


@router.post("", response_model=ShiftOut, status_code=201)
def create_shift(body: ShiftIn, request: Request, user: CurrentUser = Depends(manage)) -> ShiftOut:
    """A new shift; its times apply from the start (there are no earlier days on it)."""
    shift = make_shift(body.name, body.start, body.end, body.crosses_midnight, body.grace_min,
                       body.in_window_before_min, body.out_window_after_min)
    name_free(request, shift.name)
    shift_id = auth.db_of(request).add_shift(shift, user.username, body.reason)
    return shift_out(get_or_404(request, shift_id))


@router.patch("/{shift_id}", response_model=ShiftOut,
              responses={409: {"description": "another shift has that name"}})
def update_shift(shift_id: int, body: ShiftPatch, request: Request, user: CurrentUser = Depends(manage)) -> ShiftOut:
    """Rename the shift and/or change its times from `effective_from` (default today) on: a new version, or the one
    that already starts that day is replaced. The days before keep their times. Audited (edit_shift)."""
    db = auth.db_of(request)
    effective = body.effective_from or date.today()
    current = get_or_404(request, shift_id, effective)["shift"]
    times = {"start": body.start, "end": body.end, "grace_min": body.grace_min,
             "in_window_before_min": body.in_window_before_min, "out_window_after_min": body.out_window_after_min}
    changing_times = any(v is not None for v in times.values()) or body.crosses_midnight is not None
    rules = None
    if changing_times:
        start, end = body.start or current.start, body.end or current.end
        crosses = body.crosses_midnight if body.crosses_midnight is not None else (
            end <= start if body.start is not None or body.end is not None else current.crosses_midnight)
        rules = make_shift(current.name, start, end, crosses,
                           current.grace_min if body.grace_min is None else body.grace_min,
                           current.in_window_before_min if body.in_window_before_min is None
                           else body.in_window_before_min,
                           current.out_window_after_min if body.out_window_after_min is None
                           else body.out_window_after_min)
    if body.name is not None and body.name.strip() != current.name:
        make_shift(body.name, current.start, current.end, current.crosses_midnight, 0, 0, 0)  # the name's checks
        name_free(request, body.name.strip(), shift_id)
        db.rename_shift(shift_id, body.name.strip(), user.username, body.reason)
    if rules is not None:
        db.change_shift(shift_id, rules, effective, user.username, body.reason)
    return shift_out(get_or_404(request, shift_id))


@router.delete("/{shift_id}", status_code=204,
               responses={409: {"description": "the shift is on the roster"}})
def delete_shift(shift_id: int, request: Request, reason: str = Query(..., min_length=1, description="why"),
                 user: CurrentUser = Depends(manage)) -> None:
    """Only a shift no roster entry uses can be deleted (its days are computed from it)."""
    item = get_or_404(request, shift_id)
    if not reason.strip():
        raise HTTPException(422, "A reason is required.")
    if item["roster_days"]:
        raise HTTPException(409, f"{item['shift'].name} is on {item['roster_days']} roster day(s); change those "
                                 "first.")
    auth.db_of(request).delete_shift(shift_id, user.username, reason.strip())


@router.delete("/{shift_id}/versions/{version_id}", response_model=ShiftOut,
               responses={409: {"description": "the change has started already (or it is the first version)"}})
def withdraw_shift_change(shift_id: int, version_id: int, request: Request,
                          reason: str = Query(..., min_length=1, description="why"),
                          user: CurrentUser = Depends(manage)) -> ShiftOut:
    """Withdraw a planned change (one that starts after today). Changes in force stay: add a new one instead."""
    get_or_404(request, shift_id)
    if not reason.strip():
        raise HTTPException(422, "A reason is required.")
    try:
        auth.db_of(request).delete_shift_version(shift_id, version_id, user.username, reason.strip())
    except LookupError:
        raise HTTPException(404, f"Shift {shift_id} has no version {version_id}.") from None
    except ValueError as e:
        raise HTTPException(409, str(e)[0].upper() + str(e)[1:] + ".") from None
    return shift_out(get_or_404(request, shift_id))
