"""Shifts and the roster (app/shifts.py, app/db.py step 5): one attendance per person per roster date, from the
first IN in the shift's IN window and the last OUT in its OUT window. Rule cases on plain events first, then end to
end: camera passes through the worker's Recorder (cool-down, database check), roster rows through app.db."""

import contextlib
import io
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from app import db
from app.attendance import Event
from app.gallery import Match
from app.shifts import (
    ABSENT,
    CHECK,
    EARLY_LEAVE,
    LATE,
    MISSING_IN,
    MISSING_OUT,
    OFF_DAY,
    ON_TIME,
    UNSCHEDULED,
    Shift,
    roster_repeat_ids,
    summarize_roster,
)
from app.tracker import Sample, TrackState

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import recognize  # noqa: E402

MON = date(2026, 10, 19)  # a Monday
TUE = MON + timedelta(days=1)
CODE = "1001"
MORNING = Shift("Morning", time(6, 30), time(14, 30), False)
GENERAL = Shift("General", time(8, 0), time(16, 30), False)
NIGHT = Shift("Night", time(22, 0), time(6, 0), True)


def at(day: date, hm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hm))


def passes(*items, person=CODE) -> list[Event]:
    """(day, "HH:MM", direction) -> Events with ids 1, 2, ... in the order given."""
    return [Event(person, at(day, hm), direction, i) for i, (day, hm, direction) in enumerate(items, start=1)]


def one_day(events, shift, day=MON, now=None):
    [d] = summarize_roster(events, {(CODE, day): shift}, day, day, now)
    return d


# --- Windows -----------------------------------------------------------------------------------------------------


def test_windows_run_from_start_minus_2h_to_the_midpoint_and_on_to_end_plus_4h():
    m = MORNING.on(MON)
    assert (m.in_from, m.mid, m.out_to) == (at(MON, "04:30"), at(MON, "10:30"), at(MON, "18:30"))
    assert GENERAL.on(MON).mid == at(MON, "12:15")
    n = NIGHT.on(MON)  # dated the day it starts
    assert (n.start, n.end) == (at(MON, "22:00"), at(TUE, "06:00"))
    assert (n.in_from, n.mid, n.out_to) == (at(MON, "20:00"), at(TUE, "02:00"), at(TUE, "10:00"))


def test_a_shift_is_checked_when_made():
    with pytest.raises(ValueError, match="crosses midnight"):
        Shift("Night", time(22, 0), time(6, 0), False)
    with pytest.raises(ValueError, match="crosses midnight"):
        Shift("General", time(8, 0), time(16, 30), True)
    with pytest.raises(ValueError, match="day off"):
        Shift("off", time(8, 0), time(16, 0), False)
    with pytest.raises(ValueError, match="grace_min"):
        Shift("Long", time(8, 0), time(16, 0), False, grace_min=721)
    with pytest.raises(ValueError, match="whole minutes"):
        Shift("Odd", time(8, 0, 30), time(16, 0), False)
    assert Shift("  Day  ", time(8, 0), time(8, 0), True).on(MON).end == at(TUE, "08:00")  # 24 h, name trimmed


# --- Statuses ----------------------------------------------------------------------------------------------------


def test_on_time():
    d = one_day(passes((MON, "06:20", "IN"), (MON, "14:35", "OUT")), MORNING)
    assert (d.shift, d.status, d.first_in, d.last_out) == ("Morning", ON_TIME, at(MON, "06:20"), at(MON, "14:35"))
    assert (d.late_minutes, d.early_leave_minutes, d.overtime_minutes, d.worked_minutes) == (0, 0, 5, 495)
    assert (d.start, d.end) == (at(MON, "06:30"), at(MON, "14:30"))
    # The grace is inclusive: IN at start + grace is still on time, a minute later is late.
    assert one_day(passes((MON, "07:00", "IN"), (MON, "14:30", "OUT")), MORNING).status == ON_TIME
    assert one_day(passes((MON, "07:01", "IN"), (MON, "14:30", "OUT")), MORNING).late_minutes == 31


def test_late_at_9_00_on_morning():
    d = one_day(passes((MON, "09:00", "IN"), (MON, "14:30", "OUT")), MORNING)
    assert (d.status, d.late_minutes, d.early_leave_minutes, d.overtime_minutes) == (LATE, 150, 0, 0)
    assert d.worked_minutes == 330


def test_early_leave():
    d = one_day(passes((MON, "06:25", "IN"), (MON, "13:30", "OUT")), MORNING)
    assert (d.status, d.late_minutes, d.early_leave_minutes, d.overtime_minutes) == (EARLY_LEAVE, 0, 60, 0)
    assert one_day(passes((MON, "06:25", "IN"), (MON, "14:00", "OUT")), MORNING).status == ON_TIME  # end - grace
    both = one_day(passes((MON, "09:00", "IN"), (MON, "12:00", "OUT")), MORNING)
    assert (both.status, both.late_minutes, both.early_leave_minutes) == (LATE, 150, 150)  # late wins the status


def test_overtime_minutes():
    d = one_day(passes((MON, "07:55", "IN"), (MON, "18:45", "OUT")), GENERAL)
    assert (d.status, d.overtime_minutes, d.worked_minutes) == (ON_TIME, 135, 650)


def test_night_in_21_55_monday_and_out_06_05_tuesday_is_one_monday_row():
    events = passes((MON, "21:55", "IN"), (TUE, "06:05", "OUT"))
    days = summarize_roster(events, {(CODE, MON): NIGHT}, MON, TUE)
    assert [(d.day, d.shift, d.status) for d in days] == [(MON, "Night", ON_TIME)]
    [d] = days
    assert (d.first_in, d.last_out, d.worked_minutes, d.overtime_minutes) == (at(MON, "21:55"), at(TUE, "06:05"),
                                                                              490, 5)
    assert roster_repeat_ids(events, {(CODE, MON): NIGHT}) == set()


def test_missing_in_missing_out_and_absent():
    out_only = one_day(passes((MON, "14:40", "OUT")), MORNING)
    assert (out_only.status, out_only.late_minutes, out_only.overtime_minutes) == (MISSING_IN, None, 10)
    in_only = one_day(passes((MON, "08:00", "IN")), MORNING)
    assert (in_only.status, in_only.late_minutes, in_only.early_leave_minutes) == (MISSING_OUT, 90, None)
    assert in_only.inside and in_only.worked_minutes is None
    nothing = one_day([], MORNING)
    assert (nothing.status, nothing.first_in, nothing.last_out) == (ABSENT, None, None)
    # An OUT before the midpoint isn't the shift's OUT: the day still misses one (the pass is a repeat).
    left_early = passes((MON, "06:25", "IN"), (MON, "09:00", "OUT"))
    assert one_day(left_early, MORNING).status == MISSING_OUT
    assert roster_repeat_ids(left_early, {(CODE, MON): MORNING}) == {2}


def test_check_when_the_out_comes_before_the_in():
    d = one_day(passes((MON, "07:00", "OUT"), (MON, "12:00", "IN")), MORNING)
    assert (d.status, d.first_in, d.last_out) == (CHECK, at(MON, "12:00"), at(MON, "07:00"))
    assert (d.worked_minutes, d.late_minutes, d.early_leave_minutes, d.overtime_minutes) == (None, None, None, None)


def test_off_day_is_not_absent():
    roster = {(CODE, MON): None}
    [d] = summarize_roster([], roster, MON, MON)
    assert (d.shift, d.status) == (None, OFF_DAY)
    # Came in anyway: the passes are shown, nothing is counted.
    [d] = summarize_roster(passes((MON, "10:00", "IN"), (MON, "13:00", "OUT")), roster, MON, MON)
    assert (d.status, d.first_in, d.last_out, d.worked_minutes, d.late_minutes) == (
        OFF_DAY, at(MON, "10:00"), at(MON, "13:00"), None, None)


def test_unscheduled_passes_are_never_guessed_into_a_shift():
    events = passes((MON, "21:55", "IN"), (TUE, "06:05", "OUT"))
    days = summarize_roster(events + passes((MON, "08:00", "IN"), person="2002"), {("2002", MON): GENERAL}, MON, TUE)
    assert [(d.person, d.day, d.shift, d.status, d.first_in, d.last_out) for d in days] == [
        (CODE, MON, None, UNSCHEDULED, at(MON, "21:55"), None),
        ("2002", MON, "General", MISSING_OUT, at(MON, "08:00"), None),
        (CODE, TUE, None, UNSCHEDULED, None, at(TUE, "06:05")),
    ]
    assert all(d.worked_minutes is None and d.late_minutes is None for d in days if d.status == UNSCHEDULED)
    assert summarize_roster([], {}, MON, TUE) == []  # no roster row and no passes: nothing to report


def test_repeat_passes_are_kept_but_not_counted():
    events = passes((MON, "06:20", "IN"), (MON, "06:40", "IN"), (MON, "10:45", "OUT"), (MON, "11:15", "IN"),
                    (MON, "14:35", "OUT"), (MON, "20:00", "IN"))  # lunch out and in; a stray IN after the windows
    d = one_day(events, MORNING)
    assert (d.status, d.first_in, d.last_out) == (ON_TIME, at(MON, "06:20"), at(MON, "14:35"))
    assert roster_repeat_ids(events, {(CODE, MON): MORNING}) == {2, 3, 4, 6}
    assert not d.inside  # the 20:00 IN is after the OUT window [10:30, 18:30): not one of the shift's passes
    assert one_day(events[:4], MORNING).inside  # back from lunch at 11:15, no OUT since


def test_back_to_back_shifts_keep_their_own_passes():
    """Night on Monday, Morning on Tuesday: the 06:05 OUT is the night's, the 06:25 IN the morning's."""
    events = passes((MON, "21:55", "IN"), (TUE, "06:05", "OUT"), (TUE, "06:25", "IN"), (TUE, "14:40", "OUT"))
    days = summarize_roster(events, {(CODE, MON): NIGHT, (CODE, TUE): MORNING}, MON, TUE)
    assert [(d.shift, d.status, d.first_in, d.last_out) for d in days] == [
        ("Night", ON_TIME, at(MON, "21:55"), at(TUE, "06:05")),
        ("Morning", ON_TIME, at(TUE, "06:25"), at(TUE, "14:40"))]


def test_open_until_the_out_window_closes():
    events = passes((MON, "08:10", "IN"))
    assert one_day(events, GENERAL, now=at(MON, "12:00")).open  # still at work: may yet get an OUT
    assert one_day([], MORNING, now=at(MON, "05:00")).open  # "absent" before the shift starts
    assert not one_day(events, GENERAL, now=at(MON, "20:30")).open  # OUT window [12:15, 20:30) closed
    assert not one_day(events, GENERAL).open  # now None: every day is over


# --- Database and workers ------------------------------------------------------------------------------------------

CAMS = {"IN": {"name": "entrance", "direction": "IN"}, "OUT": {"name": "exit", "direction": "OUT"}}


@pytest.fixture
def conn():
    c = db.connect()  # the test's own data/ (tests/conftest.py)
    db.sync_persons(c, [CODE])
    yield c
    c.close()


def shift_id(conn, name: str) -> int:
    return next(s.id for s in db.list_shifts(conn) if s.name == name)


def track() -> TrackState:
    t = TrackState(1)
    for i in range(3):
        t.add(Sample(0.9, Match(CODE, CODE, 0.8 + i / 100, None, None, "match"), np.zeros((8, 8, 3), np.uint8)))
    return t


def camera_passes(items) -> list[str]:
    """(camera, time) passes through the worker's Recorder, one per camera like one worker process each; returns
    each pass's decision (event / cool-down)."""
    recorders, decisions = {}, []
    for cam, when in items:
        rec = recorders.setdefault(cam, recognize.Recorder(CAMS[cam], 0, use_db=True))
        with contextlib.redirect_stdout(io.StringIO()):
            decisions.append(rec.record(when.strftime("%H:%M:%S"), when, track(), CODE, 3)[0])
    for rec in recorders.values():
        rec.conn.close()
    return decisions


def raw_events(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute("SELECT * FROM attendance_events ORDER BY id")]


def summary(conn, first=MON, last=MON) -> list[tuple]:
    return [(d.day, d.shift, d.status, d.first_in and d.first_in.strftime("%a %H:%M"),
             d.last_out and d.last_out.strftime("%a %H:%M"), d.late_minutes, d.overtime_minutes)
            for d in db.roster_days(conn, first, last)]


def test_a_new_database_has_the_three_shifts(conn):
    assert [(s.name, f"{s.start:%H:%M}", f"{s.end:%H:%M}", s.crosses_midnight, s.grace_min, s.in_window_before_min,
             s.out_window_after_min) for s in db.list_shifts(conn)] == [
        ("Morning", "06:30", "14:30", False, 30, 120, 240),
        ("General", "08:00", "16:30", False, 30, 120, 240),
        ("Night", "22:00", "06:00", True, 30, 120, 240)]


def test_one_roster_row_per_person_per_date(conn):
    pid = db.person_ids(conn)[CODE]
    assert db.set_roster(conn, [(pid, MON, shift_id(conn, "Morning"))], "week plan", "hr1") == 1
    assert db.set_roster(conn, [(pid, MON, shift_id(conn, "Night"))], "swapped with Ali", "hr1") == 1
    assert db.set_roster(conn, [(pid, MON, shift_id(conn, "Night"))], "again", "hr1") == 0  # no change, no audit
    assert conn.execute("SELECT count(*) FROM roster").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO roster (person_id, date, shift_id, updated, updated_by) VALUES (?, ?, NULL, '', '')",
                     (pid, MON.isoformat()))
    conn.rollback()
    audit = [(a["action"], a["target"], a["details"], a["reason"]) for a in reversed(db.audit_entries(conn))]
    assert audit == [
        ("set_roster", f"roster:{CODE}:2026-10-19", '{"person": "1001", "date": "2026-10-19", "shift": [null, '
                                                    '"Morning"]}', "week plan"),
        ("set_roster", f"roster:{CODE}:2026-10-19", '{"person": "1001", "date": "2026-10-19", "shift": ["Morning", '
                                                    '"Night"]}', "swapped with Ali")]
    with pytest.raises(ValueError, match="reason"):
        db.set_roster(conn, [(pid, TUE, None)], " ", "hr1")
    with pytest.raises(ValueError, match="no shift"):
        db.set_roster(conn, [(pid, TUE, None), (pid, MON + timedelta(days=2), 999)], "plan", "hr1")
    assert db.roster_between(conn, MON, MON + timedelta(days=2)) == {(CODE, MON): db.get_shift(conn, 3)}  # all or none


def test_night_shift_through_the_cameras_is_one_monday_row(conn):
    db.set_roster(conn, [(db.person_ids(conn)[CODE], MON, shift_id(conn, "Night"))], "plan", "hr1")
    decisions = camera_passes([("IN", at(MON, "21:55")), ("IN", at(MON, "21:57")), ("OUT", at(TUE, "06:05"))])
    assert decisions == ["event", "cool-down", "event"]
    assert summary(conn, MON, TUE) == [(MON, "Night", ON_TIME, "Mon 21:55", "Tue 06:05", 0, 5)]


def test_the_cool_down_does_not_split_a_night_shift_at_midnight(conn):
    """Arriving at 23:58 and seen again at 00:02: one IN, as at any other time of day (it used to reset at midnight
    and record a second IN on the next calendar date)."""
    db.set_roster(conn, [(db.person_ids(conn)[CODE], MON, shift_id(conn, "Night"))], "plan", "hr1")
    decisions = camera_passes([("IN", at(MON, "23:58")), ("IN", at(TUE, "00:02")), ("OUT", at(TUE, "06:05"))])
    assert decisions == ["event", "cool-down", "event"]
    assert len(raw_events(conn)) == 2
    assert summary(conn, MON, TUE) == [(MON, "Night", LATE, "Mon 23:58", "Tue 06:05", 118, 5)]


def test_changing_the_roster_recalculates_from_the_raw_events(conn):
    pid = db.person_ids(conn)[CODE]
    camera_passes([("IN", at(MON, "08:05")), ("OUT", at(MON, "16:35"))])
    before = raw_events(conn)

    db.set_roster(conn, [(pid, MON, shift_id(conn, "Morning"))], "plan", "hr1")
    assert summary(conn) == [(MON, "Morning", LATE, "Mon 08:05", "Mon 16:35", 95, 125)]
    db.set_roster(conn, [(pid, MON, shift_id(conn, "General"))], "moved to General", "hr1")
    assert summary(conn) == [(MON, "General", ON_TIME, "Mon 08:05", "Mon 16:35", 0, 5)]
    db.set_roster(conn, [(pid, MON, None)], "made it a day off", "hr1")
    assert summary(conn) == [(MON, None, OFF_DAY, "Mon 08:05", "Mon 16:35", None, None)]
    assert db.clear_roster(conn, pid, MON, "not planned after all", "hr1")
    assert summary(conn) == [(MON, None, UNSCHEDULED, "Mon 08:05", "Mon 16:35", None, None)]
    assert not db.clear_roster(conn, pid, MON, "again", "hr1")

    assert raw_events(conn) == before  # the raw events never change
    assert [a["action"] for a in reversed(db.audit_entries(conn))] == ["set_roster"] * 3 + ["clear_roster"]


def test_changing_a_night_shift_also_recalculates_the_next_morning(conn):
    pid = db.person_ids(conn)[CODE]
    camera_passes([("IN", at(MON, "21:55")), ("OUT", at(TUE, "06:05"))])
    db.set_roster(conn, [(pid, MON, shift_id(conn, "Night"))], "plan", "hr1")
    assert summary(conn, MON, TUE) == [(MON, "Night", ON_TIME, "Mon 21:55", "Tue 06:05", 0, 5)]
    db.set_roster(conn, [(pid, MON, None)], "was off", "hr1")
    assert summary(conn, MON, TUE) == [(MON, None, OFF_DAY, "Mon 21:55", None, None, None),
                                       (TUE, None, UNSCHEDULED, None, "Tue 06:05", None, None)]


def test_repeat_passes_and_voided_ones(conn):
    pid = db.person_ids(conn)[CODE]
    db.set_roster(conn, [(pid, MON, shift_id(conn, "Morning"))], "plan", "hr1")
    decisions = camera_passes([("IN", at(MON, "06:20")), ("IN", at(MON, "06:22")), ("IN", at(MON, "07:10")),
                               ("OUT", at(MON, "10:45")), ("IN", at(MON, "11:15")), ("OUT", at(MON, "14:35"))])
    assert decisions == ["event", "cool-down", "event", "event", "event", "event"]  # 06:22: the same pass
    ids = {row[3][11:16]: row[0] for row in raw_events(conn)}
    assert summary(conn) == [(MON, "Morning", ON_TIME, "Mon 06:20", "Mon 14:35", 0, 5)]
    assert db.roster_repeat_event_ids(conn, MON, MON) == {ids["07:10"], ids["10:45"], ids["11:15"]}

    db.set_event_voided(conn, ids["06:20"], True, "someone else", "hr1")  # the next IN in the window takes over
    assert summary(conn) == [(MON, "Morning", LATE, "Mon 07:10", "Mon 14:35", 40, 5)]
    assert db.roster_repeat_event_ids(conn, MON, MON) == {ids["10:45"], ids["11:15"]}


def test_shifts_are_edited_with_a_reason_and_kept_while_rostered(conn):
    night = db.get_shift(conn, shift_id(conn, "Night"))
    db.save_shift(conn, Shift(night.name, night.start, night.end, True, grace_min=15, id=night.id), "policy", "admin")
    assert db.get_shift(conn, night.id).grace_min == 15
    entry = db.audit_entries(conn)[0]
    assert (entry["action"], entry["details"]) == ("edit_shift", '{"grace_min": [30, 15]}')
    with pytest.raises(ValueError, match="already exists"):
        db.save_shift(conn, Shift("morning", time(7, 0), time(15, 0), False), "dup", "admin")
    late = db.save_shift(conn, Shift("Late", time(14, 0), time(22, 0), False), "new shift", "admin")
    db.set_roster(conn, [(db.person_ids(conn)[CODE], MON, late)], "plan", "hr1")
    with pytest.raises(ValueError, match="roster"):
        db.delete_shift(conn, late, "not needed", "admin")
    db.set_roster(conn, [(db.person_ids(conn)[CODE], MON, None)], "plan changed", "hr1")
    db.delete_shift(conn, late, "not needed", "admin")
    assert [s.name for s in db.list_shifts(conn)] == ["Morning", "General", "Night"]
