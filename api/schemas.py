"""Response and request models shared by several routers, and the functions that build them.

Every endpoint declares a pydantic response model, so the OpenAPI schema (and the
TypeScript types the dashboard generates from it) is complete.
"""

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from api.db import Database, parse_ts
from app.attendance import STATUSES

# A day: complete (first IN, a later last OUT), missing exit (no OUT; for today: still inside), missing entry (no IN),
# check (the last OUT is earlier than the first IN), absent.
Status = Literal["complete", "missing exit", "missing entry", "check", "absent"]
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


# --- Daily attendance -------------------------------------------------------------------


class AttendanceRow(BaseModel):
    date: date
    person_id: int
    employee_code: str
    name: str
    department: str | None
    site_id: int | None
    first_in: datetime | None
    last_out: datetime | None
    worked_hours: float | None = Field(description="last OUT minus first IN, in hours (2 decimals); only for complete")
    worked_minutes: int | None
    status: Status
    inside: bool = Field(description="the day's last event is an IN (for today: in the building now)")


def attendance_rows(db: Database, first: date, last: date) -> list[dict]:
    """Daily rows from `first` to `last` (inclusive), computed like the dashboard's Today page
    (app.db.day_summaries: every active person, plus anyone with events that day)."""
    people = {p["employee_code"]: p for p in db.people()}
    rows = []
    day = first
    while day <= last:
        summaries, ids = db.day_summaries(day)
        for s in summaries:
            p = people.get(s.person, {})
            rows.append({
                "date": day, "person_id": ids[s.person], "employee_code": s.person, "name": p.get("name", s.person),
                "department": p.get("department"), "site_id": p.get("site_id"), "first_in": s.first_in,
                "last_out": s.last_out, "worked_hours": s.worked_hours,
                "worked_minutes": None if s.worked_hours is None else round((s.last_out - s.first_in).total_seconds() / 60),
                "status": s.status, "inside": s.inside,
            })
        day += timedelta(days=1)
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
    repeat: bool = Field(False, description="not the person's first IN or last OUT that day: kept, but the day "
                                            "doesn't use it (e.g. back in after the last OUT, a lunch break)")
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
