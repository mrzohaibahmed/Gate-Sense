"""The Shifts switch (Settings > System, /api/settings/shifts, app_settings "shifts"): the same raw events give the
calendar-day results with shifts off and the roster-day results with them on; switching changes nothing stored, is
audited, needs no restart, and only admins can do it."""

import contextlib
import csv
import io
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from app import config, db
from tests.test_data_api import as_role, at, first_password, fixed_clock, items  # noqa: F401  (autouse fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import today as today_script  # noqa: E402

D = date(2026, 10, 19)
E = D + timedelta(days=1)
TODAY = date.today()


@pytest.fixture
def app():
    application = create_app()
    with TestClient(application):
        conn = db.connect()
        db.sync_persons(conn, ["1001", "1002", "1003"])
        for code, name in (("1001", "Ayesha"), ("1002", "Bilal"), ("1003", "Chand")):
            conn.execute("UPDATE persons SET name = ? WHERE employee_code = ?", (name, code))
        conn.commit()
        ids = db.person_ids(conn)
        cin, cout = db.upsert_camera(conn, "entrance", 0, "IN"), db.upsert_camera(conn, "exit", 1, "OUT")
        shifts = {s.name: s.id for s in db.list_shifts(conn)}
        db.set_roster(conn, [(ids["1001"], D, shifts["Night"]), (ids["1002"], D, shifts["General"])], "plan", "hr")
        for code, cam, when in (("1001", cin, at(D, "21:55")), ("1001", cout, at(E, "06:05")),
                                ("1002", cin, at(D, "09:00")), ("1002", cout, at(D, "12:00")),
                                ("1002", cin, at(D, "12:30")), ("1002", cout, at(D, "16:40")),
                                ("1003", cin, at(TODAY, "08:00"))):
            db.add_event(conn, ids[code], cam, when, 0.8, None)
        conn.close()
        yield application


def stored(app) -> dict:
    with app.state.db.conn() as conn:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]
                for t in ("attendance_events", "roster", "shifts", "shift_versions")}


def rows(client, first=D, last=E) -> list[tuple]:
    got = items(client.get("/api/attendance", params={"from": first, "to": last, "sort": "date"}))
    return sorted((r["date"], r["name"], r["shift"], r["first_in"] and r["first_in"][11:16],
                   r["last_out"] and r["last_out"][11:16], r["worked_minutes"], r["late_minutes"], r["status"])
                  for r in got)


def switch(client, enabled: bool, reason="test"):
    return client.put("/api/settings/shifts", json={"enabled": enabled, "reason": reason})


def csv_rows(client, first=D, last=E) -> list[list[str]]:
    r = client.get("/api/reports/daily/export", params={"from": first, "to": last, "format": "csv"})
    return list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))


def today_py(day: date) -> str:
    buf = io.StringIO()
    sys.argv = ["today.py", "--date", day.isoformat()]
    with contextlib.redirect_stdout(buf):
        today_script.main()
    return buf.getvalue()


ON = [(str(D), "Ayesha", "Night", "21:55", "06:05", 490, 0, "on time"),
      (str(D), "Bilal", "General", "09:00", "16:40", 460, 60, "late")]
OFF = [(str(D), "Ayesha", None, "21:55", None, None, None, "missing OUT"),
       (str(D), "Bilal", None, "09:00", "16:40", 460, None, "complete"),
       (str(E), "Ayesha", None, None, "06:05", None, None, "missing IN")]


def test_the_same_raw_events_give_shift_results_on_and_calendar_results_off(app):
    admin = as_role(app, "admin")
    before = stored(app)
    assert admin.get("/api/settings/shifts").json()["enabled"] is True  # the default
    assert rows(admin) == ON

    assert switch(admin, False, "pilot without shifts").json()["enabled"] is False
    assert rows(admin) == OFF  # no restart: read on every request
    assert csv_rows(admin)[0] == ["Date", "Employee code", "Name", "Department", "First IN", "Last OUT",
                                  "Hours worked", "Status"]  # no shift columns
    monthly = {p["name"]: p for p in admin.get("/api/reports/monthly", params={"from": D, "to": E}).json()["people"]}
    assert (monthly["Ayesha"]["days"], monthly["Ayesha"]["days_present"], monthly["Ayesha"]["days_missing_out"],
            monthly["Ayesha"]["days_missing_in"]) == (2, 1, 1, 1)
    assert (monthly["Bilal"]["days_complete"], monthly["Bilal"]["days_late"]) == (1, 0)
    day = admin.get("/api/overview", params={"date": D}).json()
    assert (day["shifts"], day["people"], day["present"], day["complete"], day["missing_out"], day["absent"],
            day["late"]) == (False, 2, 2, 1, 1, 0, 0)
    out = today_py(D)
    assert "Shift" not in out and "Bilal" in out and "complete" in out and "missing OUT" in out

    # Switching back: the shift results again, from the same raw events; nothing stored changed either way.
    assert switch(admin, True, "shifts are planned now").json()["enabled"] is True
    assert rows(admin) == ON
    assert stored(app) == before
    assert "Night" in today_py(D)


def test_repeats_follow_the_rule_in_use(app):
    admin = as_role(app, "admin")

    def repeats():
        events = items(admin.get("/api/events", params={"from": D, "to": E, "sort": "timestamp"}))
        return [e["timestamp"][11:16] for e in events if e["repeat"]]

    assert repeats() == ["12:00", "12:30"]  # Bilal's lunch break
    switch(admin, False)
    assert repeats() == ["12:00", "12:30"]
    # The night shift's OUT is the Monday's with shifts on, and Tuesday's own day (missing IN) with them off.
    assert [r[7] for r in rows(admin) if r[1] == "Ayesha"] == ["missing OUT", "missing IN"]


def test_today_without_an_out_is_inside_with_shifts_off(app):
    admin = as_role(app, "admin")
    assert [r[7] for r in rows(admin, TODAY, TODAY)] == ["unscheduled"]  # no roster row: shifts on
    switch(admin, False)
    [row] = items(admin.get("/api/attendance", params={"from": TODAY, "to": TODAY}))
    assert (row["status"], row["open"], row["inside"]) == ("missing OUT", True, True)
    o = admin.get("/api/overview").json()
    assert (o["inside_now"], o["missing_out"], o["present"]) == (1, 0, 1)  # still inside: not missing yet
    assert csv_rows(admin, TODAY, TODAY)[1][-1] == "inside"
    assert "inside" in today_py(TODAY)


def test_shifts_are_off_on_a_new_install(app, monkeypatch):
    """The shipped default is off: no app_settings row means calendar days, until an admin switches shifts on."""
    from tests.conftest import SHIPPED_SHIFTS_DEFAULT

    assert SHIPPED_SHIFTS_DEFAULT is False
    monkeypatch.setattr(config, "SHIFTS_ENABLED_DEFAULT", SHIPPED_SHIFTS_DEFAULT)
    admin = as_role(app, "admin")
    assert admin.get("/api/settings/shifts").json() == {"enabled": False, "updated": None, "updated_by": None}
    assert rows(admin) == OFF
    assert admin.get("/api/roster").status_code == 409
    assert switch(admin, True, "we plan shifts now").json()["enabled"] is True
    assert rows(admin) == ON


def test_switching_is_audited_and_admin_only(app):
    admin = as_role(app, "admin")
    for role in ("hr", "viewer", "guard"):
        c = as_role(app, role)
        assert c.get("/api/settings/shifts").json()["enabled"] is True  # everyone can read it (the menu needs it)
        assert switch(c, False).status_code == 403, role
    assert switch(admin, False, " ").status_code == 422
    assert admin.get("/api/settings/shifts").json()["enabled"] is True
    switch(admin, False, "pilot")
    switch(admin, False, "again")  # already off: nothing to audit
    switch(admin, True, "back")
    entries = [e for e in app.state.db.audit_entries() if e["action"] in ("enable_shifts", "disable_shifts")]
    assert [(e["action"], e["user"], e["reason"]) for e in reversed(entries)] == [
        ("disable_shifts", "admin", "pilot"), ("enable_shifts", "admin", "back")]
    mode = admin.get("/api/settings/shifts").json()
    assert mode["updated_by"] == "admin" and mode["updated"]


def test_roster_and_shift_endpoints_wait_while_shifts_are_off(app):
    admin = as_role(app, "admin")
    switch(admin, False)
    for path in ("/api/roster", "/api/shifts", "/api/roster/import/template"):
        r = admin.get(path)
        assert r.status_code == 409 and "switched off" in r.text, path
    assert admin.put("/api/roster", json={"entries": [{"person_id": 1, "date": str(D)}],
                                          "reason": "x"}).status_code == 409
    assert TestClient(app).get("/api/roster").status_code == 401  # not logged in: still 401 first
    switch(admin, True)
    grid = {r["employee_code"]: r["cells"] for r in items(admin.get("/api/roster", params={"from": D}))}
    assert len(grid["1001"]) == 1 and len(grid["1002"]) == 1  # the roster is all there
    assert [s["name"] for s in items(admin.get("/api/shifts"))] == ["Morning", "General", "Night"]
