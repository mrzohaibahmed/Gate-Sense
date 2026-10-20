from datetime import datetime, timedelta

from app.attendance import (
    ABSENT,
    MISSING_ENTRY,
    MISSING_EXIT,
    PRESENT,
    Cooldown,
    Event,
    summarize_day,
)

T0 = datetime(2026, 10, 20, 9, 0, 0)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --- Cool-down -------------------------------------------------------------


def test_cooldown_blocks_repeat_within_window():
    cd = Cooldown(300)
    assert cd.allow("waqas", "entrance", at(0))
    assert not cd.allow("waqas", "entrance", at(1))
    assert not cd.allow("waqas", "entrance", at(4.99))


def test_cooldown_allows_after_window():
    cd = Cooldown(300)
    assert cd.allow("waqas", "entrance", at(0))
    assert cd.allow("waqas", "entrance", at(5))


def test_blocked_sightings_do_not_extend_window():
    cd = Cooldown(300)
    assert cd.allow("waqas", "entrance", at(0))
    assert not cd.allow("waqas", "entrance", at(4))
    assert cd.allow("waqas", "entrance", at(5))  # 5 min after the recorded event, not the blocked one


def test_cooldown_is_per_person_and_per_camera():
    cd = Cooldown(300)
    assert cd.allow("waqas", "entrance", at(0))
    assert cd.allow("ali", "entrance", at(1))
    assert cd.allow("waqas", "exit", at(1))


def test_cooldown_seeded_from_previous_run():
    cd = Cooldown(300, last={("waqas", "entrance"): at(0)})
    assert not cd.allow("waqas", "entrance", at(2))
    assert cd.allow("waqas", "entrance", at(6))


# --- Daily summary ---------------------------------------------------------


def summary_for(events, people=("waqas",)):
    return {s.person: s for s in summarize_day(events, list(people))}["waqas"]


def test_first_in_and_last_out():
    events = [
        Event("waqas", at(0), "IN"),
        Event("waqas", at(240), "OUT"),  # lunch
        Event("waqas", at(270), "IN"),
        Event("waqas", at(540), "OUT"),
    ]
    s = summary_for(events)
    assert s.first_in == at(0)
    assert s.last_out == at(540)
    assert s.worked_hours == 9.0
    assert s.status == PRESENT


def test_no_out_is_missing_exit():
    s = summary_for([Event("waqas", at(0), "IN")])
    assert s.status == MISSING_EXIT
    assert s.last_out is None and s.worked_hours is None


def test_returned_without_leaving_again_is_missing_exit():
    events = [Event("waqas", at(0), "IN"), Event("waqas", at(240), "OUT"), Event("waqas", at(270), "IN")]
    s = summary_for(events)
    assert s.status == MISSING_EXIT
    assert s.last_out == at(240)
    assert s.worked_hours == 4.0  # first IN to last OUT, flagged for an admin


def test_out_without_in_is_missing_entry():
    s = summary_for([Event("waqas", at(540), "OUT")])
    assert s.status == MISSING_ENTRY
    assert s.first_in is None and s.worked_hours is None


def test_enrolled_without_events_is_absent():
    s = summary_for([], people=["waqas"])
    assert s.status == ABSENT


def test_people_with_events_but_not_listed_are_included():
    summaries = summarize_day([Event("ali", at(0), "IN")], ["waqas"])
    assert [s.person for s in summaries] == ["ali", "waqas"]
