"""Response and request models shared by several routers, and the functions that build them.

Every endpoint declares a pydantic response model, so the OpenAPI schema (and the
TypeScript types the dashboard generates from it) is complete.
"""

from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from api.db import Database, parse_ts
from app.attendance import ABSENT, MISSING_ENTRY, MISSING_EXIT, PRESENT

Status = Literal["present", "missing exit", "missing entry", "absent"]
Direction = Literal["IN", "OUT"]
assert set(Status.__args__) == {PRESENT, MISSING_EXIT, MISSING_ENTRY, ABSENT}


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
    worked_hours: float | None = Field(description="last OUT minus first IN, in hours (2 decimals)")
    worked_minutes: int | None
    status: Status


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
                "status": s.status,
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
    snapshot_url: str | None = Field(description="GET it with the session cookie; None if there is no snapshot")


def event_out(row: dict) -> Event:
    return Event(
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
