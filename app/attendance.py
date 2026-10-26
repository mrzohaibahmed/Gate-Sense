"""Attendance rules for the camera workers: the cool-down, and the Event the day rules work on.

Pure logic with no database or camera access, so it is easy to test. A person's working day (first IN in the shift's
IN window, last OUT in its OUT window, statuses, repeats) is in app/shifts.py: the roster decides which shift a day
is, and a night shift is one day, dated the day it starts.
"""

from dataclasses import dataclass
from datetime import datetime


def within_cooldown(previous: datetime | None, now: datetime, seconds: float) -> bool:
    """Is `now` still inside the cool-down that started at `previous`? There is no reset at midnight: an IN at 23:58
    and another at 00:02 are one pass, as they would be at noon, so a night shift is never split by it."""
    return previous is not None and (now - previous).total_seconds() < seconds


class Cooldown:
    """Allow one event per person per direction (IN or OUT, from any camera of that direction) per cool-down
    window (no reset at midnight).

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
