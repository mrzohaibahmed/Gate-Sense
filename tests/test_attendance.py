from datetime import datetime, timedelta

from app.attendance import (
    ABSENT,
    CHECK,
    COMPLETE,
    MISSING_ENTRY,
    MISSING_EXIT,
    Cooldown,
    Event,
    repeat_ids,
    summarize_day,
)

T0 = datetime(2026, 10, 20, 9, 0, 0)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --- Cool-down: per person per direction, reset at midnight --------------------------------------------------


def test_cooldown_blocks_repeat_within_window():
    cd = Cooldown(300)
    assert cd.allow("waqas", "IN", at(0))
    assert not cd.allow("waqas", "IN", at(1))
    assert not cd.allow("waqas", "IN", at(4.99))


def test_cooldown_allows_after_window():
    cd = Cooldown(300)
    assert cd.allow("waqas", "IN", at(0))
    assert cd.allow("waqas", "IN", at(5))


def test_blocked_sightings_do_not_extend_window():
    cd = Cooldown(300)
    assert cd.allow("waqas", "IN", at(0))
    assert not cd.allow("waqas", "IN", at(4))
    assert cd.allow("waqas", "IN", at(5))  # 5 min after the recorded event, not the blocked one


def test_cooldown_is_per_person_and_per_direction():
    cd = Cooldown(300)
    assert cd.allow("waqas", "IN", at(0))
    assert cd.allow("ali", "IN", at(1))
    assert cd.allow("waqas", "OUT", at(1))  # an OUT right after an IN is a different direction


def test_cooldown_resets_at_midnight():
    cd = Cooldown(300)
    late = datetime(2026, 10, 20, 23, 58)
    assert cd.allow("waqas", "IN", late)
    assert cd.allow("waqas", "IN", late + timedelta(minutes=4))  # 00:02 the next day: a new day starts afresh


def test_cooldown_seeded_from_previous_run():
    cd = Cooldown(300, last={("waqas", "IN"): at(0)})
    assert not cd.allow("waqas", "IN", at(2))
    assert cd.allow("waqas", "IN", at(6))


def test_seen_moves_the_window_to_the_other_cameras_event():
    cd = Cooldown(300)
    assert cd.allow("waqas", "IN", at(1))  # this camera thought it was first...
    cd.seen("waqas", "IN", at(0))  # ...but another camera's event at 0 was found by the database check
    assert cd.allow("waqas", "IN", at(5))  # the window ran from at(0)


# --- Daily summary: first IN + last OUT ---------------------------------------------------------------------


def summary_for(events, people=("waqas",)):
    return {s.person: s for s in summarize_day(events, list(people))}["waqas"]


def test_first_in_and_last_out_with_a_lunch_break():
    events = [
        Event("waqas", at(0), "IN", 1),
        Event("waqas", at(240), "OUT", 2),  # lunch
        Event("waqas", at(270), "IN", 3),
        Event("waqas", at(540), "OUT", 4),
    ]
    s = summary_for(events)
    assert (s.first_in, s.last_out, s.worked_hours, s.status) == (at(0), at(540), 9.0, COMPLETE)
    assert repeat_ids(events) == {2, 3}


def test_no_out_is_missing_exit():
    s = summary_for([Event("waqas", at(0), "IN")])
    assert s.status == MISSING_EXIT and s.inside
    assert s.last_out is None and s.worked_hours is None


def test_back_in_after_the_last_out_is_a_repeat_and_the_day_stays_complete():
    events = [Event("waqas", at(0), "IN", 1), Event("waqas", at(480), "OUT", 2), Event("waqas", at(540), "IN", 3)]
    s = summary_for(events)
    assert (s.first_in, s.last_out, s.worked_hours, s.status) == (at(0), at(480), 8.0, COMPLETE)
    assert s.inside  # but the last event is an IN: in the building again
    assert repeat_ids(events) == {3}


def test_last_out_before_first_in_is_check_without_hours():
    s = summary_for([Event("waqas", at(-60), "OUT"), Event("waqas", at(0), "IN")])
    assert (s.first_in, s.last_out, s.worked_hours, s.status) == (at(0), at(-60), None, CHECK)


def test_out_without_in_is_missing_entry():
    s = summary_for([Event("waqas", at(540), "OUT")])
    assert s.status == MISSING_ENTRY and not s.inside
    assert s.first_in is None and s.worked_hours is None


def test_enrolled_without_events_is_absent():
    s = summary_for([], people=["waqas"])
    assert s.status == ABSENT and not s.inside


def test_repeats_of_one_direction_only():
    ins = [Event("waqas", at(0), "IN", 1), Event("waqas", at(6), "IN", 2)]
    assert repeat_ids(ins) == {2}  # the first IN is used
    outs = [Event("waqas", at(0), "OUT", 1), Event("waqas", at(6), "OUT", 2)]
    assert repeat_ids(outs) == {1}  # the last OUT is used


def test_people_with_events_but_not_listed_are_included():
    summaries = summarize_day([Event("ali", at(0), "IN")], ["waqas"])
    assert [s.person for s in summaries] == ["ali", "waqas"]
