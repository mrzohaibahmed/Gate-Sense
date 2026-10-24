"""Attendance rules: the cool-down and the daily summary.

Pure logic with no database or camera access, so it is easy to test.
The day is the calendar date (local midnight to midnight) for now; shift windows come later.

One attendance per person per day: the first IN and the last OUT (voided events are left out
before this). Every other event of the day is a repeat: kept in the raw table and shown in
the day's event list, but not used for the day.
"""

from dataclasses import dataclass
from datetime import datetime

COMPLETE = "complete"  # came in, and the last OUT is after the first IN
MISSING_EXIT = "missing exit"  # came in, but no OUT at all that day (today: still inside)
MISSING_ENTRY = "missing entry"  # went out, but no IN that day
CHECK = "check"  # the last OUT is earlier than the first IN: someone should look; no hours
ABSENT = "absent"
STATUSES = (COMPLETE, MISSING_EXIT, MISSING_ENTRY, CHECK, ABSENT)
CAME_IN = (COMPLETE, MISSING_EXIT, CHECK)  # "present": has an IN that day


def within_cooldown(previous: datetime | None, now: datetime, seconds: float) -> bool:
    """Is `now` still inside the cool-down that started at `previous`? A new day always starts afresh."""
    return (previous is not None and previous.date() == now.date()
            and (now - previous).total_seconds() < seconds)


class Cooldown:
    """Allow one event per person per direction (IN or OUT, from any camera of that direction) per cool-down
    window, reset at local midnight.

    The window starts at the last *recorded* event; sightings that were blocked don't extend it. This is the
    worker's quick check; the database check in app.db.add_camera_event also sees the other cameras' events.
    """

    def __init__(self, seconds: float, last: dict[tuple[str, str], datetime] | None = None):
        self.seconds = seconds
        self.last = dict(last or {})

    def allow(self, person: str, direction: str, now: datetime) -> bool:
        key = (person, direction)
        if within_cooldown(self.last.get(key), now, self.seconds):
            return False
        self.last[key] = now
        return True

    def seen(self, person: str, direction: str, when: datetime) -> None:
        """Another camera recorded this person (found by the database check): the window runs from that event, not
        from this camera's blocked sighting that allow() had already noted."""
        self.last[(person, direction)] = when


@dataclass(frozen=True)
class Event:
    person: str  # employee_code
    timestamp: datetime
    direction: str  # "IN" or "OUT"
    id: int | None = None  # the event's row id, to tell which events of the day are repeats


@dataclass(frozen=True)
class DaySummary:
    person: str
    first_in: datetime | None
    last_out: datetime | None
    worked_hours: float | None
    status: str
    inside: bool = False  # the day's last event is an IN (for today: in the building now)


def _order(e: Event):
    return e.timestamp, e.id if e.id is not None else 0


def anchors(events: list[Event]) -> tuple[Event | None, Event | None]:
    """The day's first IN and last OUT (ties by id): the two events the day is built on."""
    ins = sorted((e for e in events if e.direction == "IN"), key=_order)
    outs = sorted((e for e in events if e.direction == "OUT"), key=_order)
    return (ins[0] if ins else None), (outs[-1] if outs else None)


def repeat_ids(events: list[Event]) -> set[int]:
    """Ids of one person's events of one day that are repeats (neither the first IN nor the last OUT)."""
    used = {id(e) for e in anchors(events) if e is not None}
    return {e.id for e in events if id(e) not in used and e.id is not None}


def summarize_person(person: str, events: list[Event]) -> DaySummary:
    """First IN is the in-time, last OUT is the out-time; all events stay in the raw table."""
    first, last = anchors(events)
    first_in = first.timestamp if first else None
    last_out = last.timestamp if last else None

    if first is None and last is None:
        status = ABSENT
    elif first is None:
        status = MISSING_ENTRY
    elif last is None:
        status = MISSING_EXIT
    elif last_out < first_in:
        status = CHECK
    else:
        status = COMPLETE

    worked = round((last_out - first_in).total_seconds() / 3600, 2) if status == COMPLETE else None
    inside = bool(events) and max(events, key=_order).direction == "IN"
    return DaySummary(person, first_in, last_out, worked, status, inside)


def summarize_day(events: list[Event], people: list[str]) -> list[DaySummary]:
    """One summary per person in `people` (absent if no events), plus anyone else with events."""
    by_person: dict[str, list[Event]] = {p: [] for p in people}
    for e in events:
        by_person.setdefault(e.person, []).append(e)
    return [summarize_person(p, evs) for p, evs in sorted(by_person.items())]
