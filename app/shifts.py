"""Shifts and the roster: which passes make a person's working day, and its status. Pure logic (no database).

A roster row says which shift a person works on a date, or that the date is OFF; one row per person per date.
A shift belongs to the date it starts: Night on Monday runs from Monday 22:00 to Tuesday 06:00 and is Monday's row.
A shift's times are effective-dated: each version applies from its `effective_from` date (None = from the start)
until the next one, and a day uses the version in force on its date (`version_on`), so changing a shift's times never
rewrites the days before the change.

Each rostered shift has two windows, both half-open [from, to):
- IN window: from `in_window_before_min` before the start to the shift's midpoint;
- OUT window: from the midpoint to `out_window_after_min` after the end.

The day's IN is the first IN pass in its IN window, its OUT the last OUT pass in its OUT window (voided events are
left out before this). Someone who left before the OUT window opened (an IN, then an OUT before the midpoint and none
in the OUT window) has their last OUT after the IN as the day's OUT: an early leave. A pass inside a shift's span (IN
window start to OUT window end) belongs to that shift; when two of the person's shifts could take it, the one where
its direction fits the window wins, then the one whose start (for an IN) or end (for an OUT) is nearer. Every pass a
day doesn't use is a repeat: kept, listed, never counted.

Nothing is stored: the days are computed from the raw events and the roster each time, so changing a roster row
recalculates the days around it, and the raw events never change.

Status, one per day (the minutes fields carry the rest, e.g. a late arrival who also left early):
- on time: IN at or before start + grace (and the OUT, if any, not an early leave);
- late: IN after start + grace (`late_minutes` = minutes after the start);
- early leave: on time, but OUT before end - grace (`early_leave_minutes` = minutes before the end);
- missing IN: an OUT in the OUT window but no IN in the IN window;
- missing OUT: an IN, no OUT after it, and the OUT window has closed;
- absent: rostered, the IN window has closed, and no pass in the shift's span;
- not in yet: rostered, no IN yet, and the IN window is still open (at `now`);
- check: passes in the shift's span, but none where it counts (no IN in the IN window and no OUT in the OUT window,
  e.g. an OUT before the IN): someone should look;
- off day: the roster says OFF (never absent); passes that day are shown, not counted;
- unscheduled: passes on a date the person has no roster row for. Nothing is guessed: no shift, no minutes.
While the OUT window is open, a day with an IN and no OUT yet is on time or late (still at work), never missing OUT.
`overtime_minutes` = minutes the OUT is after the end (0 if not). `open`: the OUT window hasn't closed yet at `now`,
so the day may still change.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app import config
from app.attendance import Event

ON_TIME = "on time"
LATE = "late"
EARLY_LEAVE = "early leave"
MISSING_IN = "missing IN"
MISSING_OUT = "missing OUT"
ABSENT = "absent"
NOT_IN_YET = "not in yet"
CHECK = "check"
OFF_DAY = "off day"
UNSCHEDULED = "unscheduled"
STATUSES = (ON_TIME, LATE, EARLY_LEAVE, MISSING_IN, MISSING_OUT, ABSENT, NOT_IN_YET, CHECK, OFF_DAY, UNSCHEDULED)
CAME_IN = (ON_TIME, LATE, EARLY_LEAVE, MISSING_OUT)  # "present": an IN in the shift's IN window
NEEDS_ATTENTION = (MISSING_IN, MISSING_OUT, CHECK)  # someone should look at the events

OFF = "OFF"  # the roster's day off; never a shift's name
# A shift's span is at most 48 h (up to 24 h long, windows up to SHIFT_MINUTES_MAX = 12 h each side), so the shifts
# that can take a pass on a date are dated at most 3 days around it: a range needs the roster and events this far out.
ROSTER_PAD_DAYS = 3


@dataclass(frozen=True)
class Shift:
    """One version of a shift: its name and the times and windows in force from `effective_from`."""
    name: str
    start: time
    end: time
    crosses_midnight: bool
    grace_min: int = config.SHIFT_GRACE_MIN
    in_window_before_min: int = config.SHIFT_IN_WINDOW_BEFORE_MIN
    out_window_after_min: int = config.SHIFT_OUT_WINDOW_AFTER_MIN
    id: int | None = None  # the shift's id (the same for all its versions)
    effective_from: date | None = None  # None = from the start (the first version)

    def __post_init__(self):
        name = (self.name or "").strip()
        if not name:
            raise ValueError("a shift needs a name")
        if name.upper() == OFF:
            raise ValueError(f"{OFF} is the roster's day off, not a shift")
        object.__setattr__(self, "name", name)
        for t in (self.start, self.end):
            if t.second or t.microsecond or t.tzinfo is not None:
                raise ValueError("shift times are whole minutes, without a time zone")
        if self.crosses_midnight != (self.end <= self.start):
            raise ValueError("a shift crosses midnight exactly when it ends at or before its start time "
                             f"({self.start:%H:%M}-{self.end:%H:%M})")
        for field in ("grace_min", "in_window_before_min", "out_window_after_min"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= config.SHIFT_MINUTES_MAX:
                raise ValueError(f"{field} must be a whole number of minutes from 0 to {config.SHIFT_MINUTES_MAX}")

    def rules(self) -> tuple:
        """What a version decides (not the name, id or date): two versions with the same rules are the same."""
        return (self.start, self.end, self.crosses_midnight, self.grace_min, self.in_window_before_min,
                self.out_window_after_min)

    def on(self, day: date) -> "ShiftTimes":
        """This shift on `day` (the date it starts): planned start and end, midpoint, windows."""
        start = datetime.combine(day, self.start)
        end = datetime.combine(day + timedelta(days=1 if self.crosses_midnight else 0), self.end)
        mid = start + (end - start) / 2
        return ShiftTimes(self, day, start, end, mid, start - timedelta(minutes=self.in_window_before_min),
                          end + timedelta(minutes=self.out_window_after_min))


def version_on(versions: list[Shift], day: date) -> Shift:
    """The version in force on `day`: the latest whose effective_from is on or before it. `versions` are one shift's,
    oldest first (the first one, from the start, has effective_from None)."""
    current = versions[0]
    for v in versions:
        if v.effective_from is None or v.effective_from <= day:
            current = v
    return current


@dataclass(frozen=True)
class ShiftTimes:
    shift: Shift
    day: date
    start: datetime
    end: datetime
    mid: datetime
    in_from: datetime  # IN window [in_from, mid)
    out_to: datetime  # OUT window [mid, out_to)

    def in_window(self, t: datetime) -> bool:
        return self.in_from <= t < self.mid

    def out_window(self, t: datetime) -> bool:
        return self.mid <= t < self.out_to

    def spans(self, t: datetime) -> bool:
        return self.in_from <= t < self.out_to

    def fits(self, e: Event) -> bool:
        return self.in_window(e.timestamp) if e.direction == "IN" else self.out_window(e.timestamp)


@dataclass(frozen=True)
class ShiftDay:
    person: str  # employee_code
    day: date  # the roster date (a night shift: the date it starts); unscheduled: the calendar date of the passes
    shift: str | None  # the shift's name; None for an off day or unscheduled
    status: str
    first_in: datetime | None = None
    last_out: datetime | None = None
    start: datetime | None = None  # the shift's planned start and end (None: off day, unscheduled)
    end: datetime | None = None
    worked_minutes: int | None = None  # last OUT - first IN, when both count
    late_minutes: int | None = None  # after the start; 0 = within the grace; None = no IN in the window
    early_leave_minutes: int | None = None  # before the end; 0 = not before end - grace; None = no OUT counted
    overtime_minutes: int | None = None  # after the end; 0 = not after it; None = no OUT counted
    inside: bool = False  # the last of the day's passes is an IN
    open: bool = False  # the day can still change at `now` (its OUT window, or for off/unscheduled the date, runs on)
    shift_id: int | None = None


def _order(e: Event):
    return e.timestamp, e.id if e.id is not None else 0


def _minutes(delta: timedelta) -> int:
    """Whole minutes (rounded down)."""
    return int(delta.total_seconds() // 60)


def _times_for(e: Event, times: list[ShiftTimes]) -> ShiftTimes | None:
    """The rostered shift this pass belongs to, or None (see the module docstring)."""
    candidates = [t for t in times if t.spans(e.timestamp)]
    if not candidates:
        return None

    def preference(t: ShiftTimes):
        anchor = t.start if e.direction == "IN" else t.end
        return not t.fits(e), abs(e.timestamp - anchor), t.day

    return min(candidates, key=preference)


def _shift_day(person: str, t: ShiftTimes, events: list[Event], now: datetime | None, used: set) -> ShiftDay:
    first = next((e for e in events if e.direction == "IN" and t.in_window(e.timestamp)), None)
    last = next((e for e in reversed(events) if e.direction == "OUT" and t.out_window(e.timestamp)), None)
    if first is not None and last is None:  # left before the OUT window opened: their last OUT after the IN
        last = next((e for e in reversed(events) if e.direction == "OUT" and e.timestamp > first.timestamp), None)
    in_open = now is not None and now < t.mid
    out_open = now is not None and now < t.out_to
    late = early = overtime = worked = None
    if first is None and last is None:
        if in_open:
            status = NOT_IN_YET
        elif events:  # passes, but none where it counts: show the first IN and last OUT of the span for whoever looks
            status = CHECK
            first = next((e for e in events if e.direction == "IN"), None)
            last = next((e for e in reversed(events) if e.direction == "OUT"), None)
        else:
            status = ABSENT
    else:
        grace = timedelta(minutes=t.shift.grace_min)
        if first is not None:
            late = _minutes(first.timestamp - t.start) if first.timestamp > t.start + grace else 0
        if last is not None:
            early = _minutes(t.end - last.timestamp) if last.timestamp < t.end - grace else 0
            overtime = max(0, _minutes(last.timestamp - t.end))
        if last is None:
            status = (LATE if late else ON_TIME) if out_open else MISSING_OUT
        elif first is None:
            status = MISSING_IN
        else:
            status = LATE if late else EARLY_LEAVE if early else ON_TIME
            worked = _minutes(last.timestamp - first.timestamp)
    used.update(e.id for e in (first, last) if e is not None and e.id is not None)
    return ShiftDay(person, t.day, t.shift.name, status,
                    first.timestamp if first else None, last.timestamp if last else None, t.start, t.end,
                    worked, late, early, overtime, inside=bool(events) and events[-1].direction == "IN",
                    open=out_open, shift_id=t.shift.id)


def _loose_day(person: str, day: date, status: str, events: list[Event], now: datetime | None, used: set) -> ShiftDay:
    """An off day or an unscheduled date: its passes (those no shift took) are shown, nothing is counted."""
    first = next((e for e in events if e.direction == "IN"), None)
    last = next((e for e in reversed(events) if e.direction == "OUT"), None)
    used.update(e.id for e in (first, last) if e is not None and e.id is not None)
    return ShiftDay(person, day, None, status, first.timestamp if first else None, last.timestamp if last else None,
                    inside=bool(events) and events[-1].direction == "IN",
                    open=now is not None and now < datetime.combine(day + timedelta(days=1), time()))


def _evaluate(events: Iterable[Event], roster: Mapping[tuple[str, date], Shift | None],
              now: datetime | None) -> tuple[dict[tuple[str, date], ShiftDay], set[int]]:
    """Every roster row's day and every unscheduled (person, date) with passes; and the ids of the passes they use."""
    times: dict[str, list[ShiftTimes]] = {}
    for (person, day), shift in roster.items():
        if shift is not None:
            times.setdefault(person, []).append(shift.on(day))
    taken: dict[tuple[str, date], list[Event]] = {}
    loose: dict[tuple[str, date], list[Event]] = {}
    for e in sorted(events, key=_order):
        t = _times_for(e, times.get(e.person, []))
        if t is None:
            loose.setdefault((e.person, e.timestamp.date()), []).append(e)
        else:
            taken.setdefault((e.person, t.day), []).append(e)
    days: dict[tuple[str, date], ShiftDay] = {}
    used: set[int] = set()
    for (person, day), shift in roster.items():
        if shift is None:
            days[person, day] = _loose_day(person, day, OFF_DAY, loose.get((person, day), []), now, used)
        else:
            days[person, day] = _shift_day(person, shift.on(day), taken.get((person, day), []), now, used)
    for (person, day), evs in loose.items():
        if (person, day) not in roster:
            days[person, day] = _loose_day(person, day, UNSCHEDULED, evs, now, used)
    return days, used


def summarize_roster(events: Iterable[Event], roster: Mapping[tuple[str, date], Shift | None], first: date,
                     last: date, now: datetime | None = None) -> list[ShiftDay]:
    """The days from `first` to `last` (inclusive), by date then person: one per roster row (a shift, or OFF = None),
    plus one "unscheduled" day per person and date with passes but no roster row. Nobody else gets a day.

    `roster` must also hold the rows ROSTER_PAD_DAYS around the range (each the version in force on its date), and
    `events` the non-voided passes of those dates, because a shift there can take a pass made inside the range
    (app.db.roster_days fetches them). `now` None = every day is over (nothing is `open` or "not in yet").
    """
    days, _ = _evaluate(events, roster, now)
    return sorted((d for d in days.values() if first <= d.day <= last), key=lambda d: (d.day, d.person))


def roster_repeat_ids(events: Iterable[Event], roster: Mapping[tuple[str, date], Shift | None],
                     now: datetime | None = None) -> set[int]:
    """Ids of the passes no day uses (not a shift's first IN or last OUT, nor what an off day or an unscheduled
    date shows): kept and listed, but never counted."""
    events = list(events)
    _, used = _evaluate(events, roster, now)
    return {e.id for e in events if e.id is not None and e.id not in used}
