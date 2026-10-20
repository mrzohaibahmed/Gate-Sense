"""Attendance rules: the per-camera cool-down and the daily summary.

Pure logic with no database or camera access, so it is easy to test.
The day is the calendar date for now; shift windows come later.
"""

from dataclasses import dataclass
from datetime import datetime

PRESENT = "present"
MISSING_EXIT = "missing exit"  # came in, but no OUT after the last IN
MISSING_ENTRY = "missing entry"  # went out without an IN that day
ABSENT = "absent"


class Cooldown:
    """Allow one event per (person, camera) per cool-down window.

    The window starts at the last *recorded* event; sightings that were
    blocked don't extend it.
    """

    def __init__(self, seconds: float, last: dict[tuple[str, str], datetime] | None = None):
        self.seconds = seconds
        self.last = dict(last or {})

    def allow(self, person: str, camera: str, now: datetime) -> bool:
        key = (person, camera)
        prev = self.last.get(key)
        if prev is not None and (now - prev).total_seconds() < self.seconds:
            return False
        self.last[key] = now
        return True


@dataclass(frozen=True)
class Event:
    person: str  # employee_code
    timestamp: datetime
    direction: str  # "IN" or "OUT"


@dataclass(frozen=True)
class DaySummary:
    person: str
    first_in: datetime | None
    last_out: datetime | None
    worked_hours: float | None
    status: str


def summarize_person(person: str, events: list[Event]) -> DaySummary:
    """First IN is the in-time, last OUT is the out-time; all events stay in the raw table."""
    ins = [e.timestamp for e in events if e.direction == "IN"]
    outs = [e.timestamp for e in events if e.direction == "OUT"]
    first_in = min(ins) if ins else None
    last_out = max(outs) if outs else None

    if not ins and not outs:
        status = ABSENT
    elif not ins:
        status = MISSING_ENTRY
    elif last_out is None or last_out < max(ins):
        status = MISSING_EXIT
    else:
        status = PRESENT

    worked = None
    if first_in and last_out and last_out > first_in:
        worked = round((last_out - first_in).total_seconds() / 3600, 2)
    return DaySummary(person, first_in, last_out, worked, status)


def summarize_day(events: list[Event], people: list[str]) -> list[DaySummary]:
    """One summary per person in `people` (absent if no events), plus anyone else with events."""
    by_person: dict[str, list[Event]] = {p: [] for p in people}
    for e in events:
        by_person.setdefault(e.person, []).append(e)
    return [summarize_person(p, evs) for p, evs in sorted(by_person.items())]
