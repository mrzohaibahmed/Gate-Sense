"""Response and request models shared by several routers, and the functions that build them.

Every endpoint declares a pydantic response model, so the OpenAPI schema (and the
TypeScript types the dashboard generates from it) is complete.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from api.db import Database, parse_ts
from app.shifts import STATUSES

# A roster day's status (app/shifts.py): on time, late, early leave, missing IN, missing OUT, absent, not in yet (the IN
# window is still open), check (passes, but none where they count), off day (OFF on the roster), unscheduled (passes
# on a date with no roster row).
Status = Literal["on time", "late", "early leave", "missing IN", "missing OUT", "absent", "not in yet", "check",
                 "off day", "unscheduled"]
Direction = Literal["IN", "OUT"]
assert set(Status.__args__) == set(STATUSES)


class OptionalReason(BaseModel):
    """Enrollment (add a person, save photos): the reason may be left out; the audit log then gets a default one."""
    reason: str | None = Field(None, description="why (goes to the audit log); a default is used when left out")

    def reason_or(self, default: str) -> str:
        return (self.reason or "").strip() or default


class Reason(BaseModel):
    reason: str = Field(min_length=1, description="why (goes to the audit log)")

    @field_validator("reason")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a reason is required")
        return v.strip()


class Message(BaseModel):
    detail: str


# --- Daily attendance: roster days -------------------------------------------------------------------------------


class AttendanceRow(BaseModel):
    date: Annotated[date, Field(description="the roster date (a night shift: the day it starts); unscheduled: the "
                                            "calendar date of the passes")]
    person_id: int
    employee_code: str
    name: str
    department: str | None
    site_id: int | None
    shift_id: int | None
    shift: str | None = Field(description="the rostered shift's name; None for an off day or unscheduled")
    shift_start: datetime | None = Field(description="the shift's planned start (its times in force that day)")
    shift_end: datetime | None
    first_in: datetime | None = Field(description="the first IN in the shift's IN window (off day, unscheduled: the "
                                                  "first IN that date)")
    last_out: datetime | None = Field(description="the last OUT in the OUT window, or for an early leave before the "
                                                  "midpoint the last OUT after the IN")
    worked_hours: float | None = Field(description="last OUT minus first IN, in hours (2 decimals), when both count")
    worked_minutes: int | None
    late_minutes: int | None = Field(description="minutes after the start (0 = within the grace); None = no IN")
    early_leave_minutes: int | None = Field(description="minutes before the end (0 = not early); None = no OUT")
    overtime_minutes: int | None = Field(description="minutes after the end (0 = none); None = no OUT")
    status: Status
    inside: bool = Field(description="the day's last pass is an IN (while open: in the building now)")
    open: bool = Field(description="the day can still change: its OUT window (off day, unscheduled: the date) hasn't "
                                   "ended")


def attendance_rows(db: Database, first: date, last: date, now: datetime | None = None) -> list[dict]:
    """Roster days from `first` to `last` (inclusive), the same as every screen, report and scripts/today.py
    (app.db.roster_days: one per roster row, plus unscheduled dates with passes)."""
    people = {p["employee_code"]: p for p in db.people()}
    rows = []
    for d in db.roster_days(first, last, now):
        p = people[d.person]
        rows.append({
            "date": d.day, "person_id": p["id"], "employee_code": d.person, "name": p["name"],
            "department": p["department"], "site_id": p["site_id"], "shift_id": d.shift_id, "shift": d.shift,
            "shift_start": d.start, "shift_end": d.end, "first_in": d.first_in, "last_out": d.last_out,
            "worked_hours": None if d.worked_minutes is None else round(d.worked_minutes / 60, 2),
            "worked_minutes": d.worked_minutes, "late_minutes": d.late_minutes,
            "early_leave_minutes": d.early_leave_minutes, "overtime_minutes": d.overtime_minutes,
            "status": d.status, "inside": d.inside, "open": d.open,
        })
    return rows


# --- Raw events -----------------------------------------------------------------------------


class Event(BaseModel):
    id: int
    timestamp: datetime
    original_timestamp: datetime | None = Field(description="the time before the first correction, if corrected")
    person_id: int
    employee_code: str
    name: str
    camera_id: int
    camera: str
    direction: Direction
    site_id: int | None
    score: float | None = Field(description="match score; None for a manual event")
    manual: bool
    voided: bool
    repeat: bool = Field(False, description="no roster day uses it (not the shift's first IN or last OUT): kept, "
                                            "but not counted (e.g. a lunch break, a pass outside the windows)")
    snapshot_url: str | None = Field(description="GET it with the session cookie; None if there is no snapshot")


def event_out(row: dict, repeats: set[int] | frozenset = frozenset()) -> Event:
    """`repeats`: ids of repeat events (Database.repeat_ids of the rows being returned)."""
    return Event(repeat=row["id"] in repeats,
        id=row["id"], timestamp=parse_ts(row["timestamp"]), original_timestamp=parse_ts(row["original_timestamp"]),
        person_id=row["person_id"], employee_code=row["employee_code"], name=row["name"], camera_id=row["camera_id"],
        camera=row["camera"], direction=row["direction"], site_id=row["site_id"],
        score=None if row["manual"] else round(row["match_score"], 3), manual=bool(row["manual"]),
        voided=bool(row["voided"]), snapshot_url=f"/api/snapshots/{row['id']}" if row["snapshot_path"] else None)


def day_range(date_from: date | None, date_to: date | None, max_days: int = 366) -> tuple[date, date]:
    """from/to query parameters, inclusive: neither = today, only from = from..today, only to = that day.
    ValueError if reversed or longer than `max_days`."""
    today = date.today()
    first = date_from or date_to or today
    last = date_to or max(first, today)
    if last < first:
        raise ValueError("'to' is before 'from'")
    if (last - first).days >= max_days:
        raise ValueError(f"at most {max_days} days at a time")
    return first, last
