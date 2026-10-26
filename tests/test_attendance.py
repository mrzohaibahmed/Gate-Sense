"""The workers' cool-down (app.attendance). The day rules (first IN, last OUT, statuses, repeats) are tested in
tests/test_shifts.py."""

from datetime import datetime, timedelta

from app.attendance import Cooldown

T0 = datetime(2026, 10, 20, 9, 0, 0)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --- Cool-down: per person per direction, no reset at midnight --------------------------------------------------


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


def test_cooldown_does_not_reset_at_midnight():
    """A night shift's passes either side of midnight are one cool-down, as they would be at noon."""
    cd = Cooldown(300)
    late = datetime(2026, 10, 20, 23, 58)
    assert cd.allow("waqas", "IN", late)
    assert not cd.allow("waqas", "IN", late + timedelta(minutes=4))  # 00:02 the next day: still the same pass
    assert cd.allow("waqas", "IN", late + timedelta(minutes=5))


def test_cooldown_seeded_from_previous_run():
    cd = Cooldown(300, last={("waqas", "IN"): at(0)})
    assert not cd.allow("waqas", "IN", at(2))
    assert cd.allow("waqas", "IN", at(6))


def test_seen_moves_the_window_to_the_other_cameras_event():
    cd = Cooldown(300)
    assert cd.allow("waqas", "IN", at(1))  # this camera thought it was first...
    cd.seen("waqas", "IN", at(0))  # ...but another camera's event at 0 was found by the database check
    assert cd.allow("waqas", "IN", at(5))  # the window ran from at(0)
