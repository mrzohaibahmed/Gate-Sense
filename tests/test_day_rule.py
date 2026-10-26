"""One attendance per person per day (first IN + last OUT), proven end to end on the audit's cases a-h and the extra
"last OUT before first IN" case: camera passes go through the worker's Recorder (cool-down per person per direction,
the database check), corrections through the API, and every screen's data, the reports, the CSV/XLSX exports and
scripts/today.py are read back."""

import contextlib
import csv
import io
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from api.main import create_app
from app import db
from app.gallery import Match
from app.tracker import Sample, TrackState
from tests.test_data_api import as_role, first_password  # noqa: F401  (first_password: autouse fixture)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import recognize  # noqa: E402
import today as today_script  # noqa: E402

D = date(2026, 10, 20)
TODAY = date.today()
CODE = "1001"
CAMS = {"IN": {"name": "entrance", "direction": "IN"}, "OUT": {"name": "exit", "direction": "OUT"},
        "IN2": {"name": "entrance2", "direction": "IN"}}


def at(day: date, hms: str, days: int = 0) -> datetime:
    return datetime.combine(day + timedelta(days=days), datetime.strptime(hms, "%H:%M:%S").time())


def track() -> TrackState:
    t = TrackState(1)
    for i in range(3):
        t.add(Sample(0.9, Match(CODE, CODE, 0.8 + i / 100, None, None, "match"), np.zeros((8, 8, 3), np.uint8)))
    return t


def run_case(passes, first: date, last: date, corrections=()) -> dict:
    """Camera passes through the worker's Recorder (one per camera, like one worker process each), corrections through
    the API; returns what every screen, report and export shows."""
    conn = db.connect()
    db.sync_persons(conn, [CODE])
    conn.close()
    recorders, decisions = {}, []
    for cam, when in passes:
        rec = recorders.setdefault(cam, recognize.Recorder(CAMS[cam], 0, use_db=True))
        with contextlib.redirect_stdout(io.StringIO()):
            decisions.append(rec.record(when.strftime("%H:%M:%S"), when, track(), CODE, 3)[0])
    for rec in recorders.values():
        rec.conn.close()
    app = create_app()
    out = {"decisions": decisions}
    with TestClient(app):
        hr = as_role(app, "hr")
        for corr in corrections:
            body = dict(corr, reason="audit case")
            if "event_at" in body:
                wanted = body.pop("event_at")
                events = hr.get("/api/events", params={"from": first, "to": last}).json()["items"]
                body["event_id"] = next(e["id"] for e in events if e["timestamp"].endswith(wanted))
            assert hr.post("/api/attendance/corrections", json=body).status_code == 201
        events = hr.get("/api/events", params={"from": first, "to": last, "sort": "timestamp"}).json()["items"]
        out["events"] = [(e["timestamp"][11:19], e["direction"], e["repeat"], e["voided"], e["manual"]) for e in events]
        rows = hr.get("/api/attendance", params={"from": first, "to": last, "sort": "date"}).json()["items"]
        out["rows"] = [(r["first_in"] and r["first_in"][11:19], r["last_out"] and r["last_out"][11:19],
                        r["worked_hours"], r["status"]) for r in rows]
        out["overview"] = []
        day = first
        while day <= last:
            o = hr.get("/api/overview", params={"date": day}).json()
            out["overview"].append({k: o[k] for k in ("present", "inside_now", "complete", "missing_exit",
                                                      "missing_entry", "check", "absent")})
            day += timedelta(days=1)
        daily = hr.get("/api/reports/daily", params={"from": first, "to": last}).json()["rows"]
        out["daily"] = [(r["status"], r["missing_out"], r["check"], r["worked_hours"]) for r in daily]
        [person] = hr.get("/api/reports/monthly", params={"from": first, "to": last}).json()["people"]
        out["monthly"] = {k: person[k] for k in ("days", "days_present", "days_missing_exit", "days_missing_entry",
                                                 "days_check", "total_hours")}
        text = hr.get("/api/reports/daily/export", params={"from": first, "to": last, "format": "csv"})
        lines = list(csv.reader(io.StringIO(text.content.decode("utf-8-sig"))))
        assert lines[0][-3:] == ["Missing OUT", "Check", "Status"]
        out["csv"] = [line[-3:] for line in lines[1:]]
        book = load_workbook(io.BytesIO(hr.get("/api/reports/monthly/export",
                                               params={"from": first, "to": last, "format": "xlsx"}).content))
        out["xlsx"] = [list(row[-3:]) for row in book["Daily detail"].iter_rows(min_row=2, values_only=True)]
        summary = [c.value for c in book["Summary"][1]]
        out["xlsx_check_days"] = book["Summary"].cell(row=2, column=summary.index("Days to check") + 1).value
    out["today_py"] = []
    day = first
    while day <= last:
        buf = io.StringIO()
        sys.argv = ["today.py", "--date", day.isoformat()]
        with contextlib.redirect_stdout(buf):
            today_script.main()
        lines = buf.getvalue().splitlines()
        status_at = next(line for line in lines if line.startswith("Name")).index("Status")
        out["today_py"] += [line[status_at:].strip() for line in lines if line.startswith(CODE)]
        day += timedelta(days=1)
    return out


def same_everywhere(out, statuses):
    """The status of each day agrees across the API, the register, the CSV, the Excel sheet and today.py."""
    assert [r[3] for r in out["rows"]] == [d[0] for d in out["daily"]] == statuses
    exported = ["inside" if s == "missing exit" and day == TODAY else s for s, day in statuses_days(out, statuses)]
    assert [c[2] for c in out["csv"]] == [x[2] for x in out["xlsx"]] == out["today_py"] == exported


def statuses_days(out, statuses):
    return list(zip(statuses, out["days"]))


def case(passes, first=D, last=D, corrections=()):
    out = run_case(passes, first, last, corrections)
    out["days"] = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return out


def test_a_in_and_out():
    out = case([("IN", at(D, "08:00:00")), ("OUT", at(D, "16:00:00"))])
    assert out["rows"] == [("08:00:00", "16:00:00", 8.0, "complete")]
    assert [e[2] for e in out["events"]] == [False, False]  # nothing is a repeat
    assert out["overview"] == [{"present": 1, "inside_now": None, "complete": 1, "missing_exit": 0,
                                "missing_entry": 0, "check": 0, "absent": 0}]
    assert out["daily"] == [("complete", False, False, 8.0)] and out["csv"] == [["", "", "complete"]]
    assert out["monthly"] == {"days": 1, "days_present": 1, "days_missing_exit": 0, "days_missing_entry": 0,
                              "days_check": 0, "total_hours": 8.0}
    same_everywhere(out, ["complete"])


def test_b_back_in_after_the_last_out_is_a_repeat():
    out = case([("IN", at(D, "08:00:00")), ("OUT", at(D, "16:00:00")), ("IN", at(D, "17:00:00"))])
    assert out["rows"] == [("08:00:00", "16:00:00", 8.0, "complete")]
    assert out["events"] == [("08:00:00", "IN", False, False, False), ("16:00:00", "OUT", False, False, False),
                             ("17:00:00", "IN", True, False, False)]
    assert out["daily"] == [("complete", False, False, 8.0)]
    assert out["overview"][0]["complete"] == 1 and out["overview"][0]["missing_exit"] == 0
    assert out["monthly"]["total_hours"] == 8.0 and out["monthly"]["days_missing_exit"] == 0
    same_everywhere(out, ["complete"])


def test_c_standing_in_front_of_the_camera_records_one_in():
    out = case([("IN", at(D, "08:00:00")), ("IN", at(D, "08:00:20"))])
    assert out["decisions"] == ["event", "cool-down"]
    assert [e[:2] for e in out["events"]] == [("08:00:00", "IN")]
    assert out["rows"] == [("08:00:00", None, None, "missing exit")]
    assert out["daily"] == [("missing exit", True, False, None)] and out["csv"] == [["YES", "", "missing exit"]]
    same_everywhere(out, ["missing exit"])


def test_c2_a_second_in_camera_is_in_the_same_cool_down():
    out = case([("IN", at(D, "08:00:00")), ("IN2", at(D, "08:00:20"))])
    assert out["decisions"] == ["event", "cool-down"]  # another camera, same direction: one IN
    assert [e[:2] for e in out["events"]] == [("08:00:00", "IN")]


def test_c3_after_the_cool_down_a_second_in_is_kept_as_a_repeat():
    out = case([("IN", at(D, "08:00:00")), ("IN", at(D, "08:06:00"))])
    assert [(e[0], e[2]) for e in out["events"]] == [("08:00:00", False), ("08:06:00", True)]
    assert out["rows"] == [("08:00:00", None, None, "missing exit")]


def test_d_lunch_break_is_inside_the_day():
    out = case([("IN", at(D, "08:00:00")), ("OUT", at(D, "12:00:00")), ("IN", at(D, "13:00:00")),
                ("OUT", at(D, "16:00:00"))])
    assert out["rows"] == [("08:00:00", "16:00:00", 8.0, "complete")]
    assert [e[2] for e in out["events"]] == [False, True, True, False]  # the lunch OUT and IN are repeats
    same_everywhere(out, ["complete"])


def test_e_seen_only_by_the_exit_camera():
    out = case([("OUT", at(D, "08:00:00"))])
    assert out["rows"] == [(None, "08:00:00", None, "missing entry")]
    assert out["overview"][0] == {"present": 0, "inside_now": None, "complete": 0, "missing_exit": 0,
                                  "missing_entry": 1, "check": 0, "absent": 0}
    assert out["monthly"]["days_present"] == 0 and out["monthly"]["days_missing_entry"] == 1
    same_everywhere(out, ["missing entry"])


def test_f_in_only_on_a_past_day():
    out = case([("IN", at(D, "08:00:00"))])
    assert out["rows"] == [("08:00:00", None, None, "missing exit")]
    assert out["daily"] == [("missing exit", True, False, None)] and out["csv"] == [["YES", "", "missing exit"]]
    assert out["monthly"]["days_present"] == 1 and out["monthly"]["days_missing_exit"] == 1
    same_everywhere(out, ["missing exit"])


def test_f_in_only_today_reads_inside_everywhere():
    out = case([("IN", at(TODAY, "00:00:01"))], TODAY, TODAY)
    assert out["rows"] == [("00:00:01", None, None, "missing exit")]
    assert out["overview"][0]["inside_now"] == 1 and out["overview"][0]["present"] == 1
    assert out["daily"] == [("missing exit", False, False, None)]  # not "missing" while the day is still running
    assert out["csv"] == [["", "", "inside"]] and out["xlsx"] == [[None, None, "inside"]]
    assert out["today_py"] == ["inside"] and out["monthly"]["days_missing_exit"] == 0
    same_everywhere(out, ["missing exit"])


def test_g_night_shift_is_still_two_days_on_the_calendar_screens():
    """The screens and reports still use the calendar day; the roster rule (app/shifts.py) makes this one day dated
    the day the shift starts (tests/test_shifts.py), and step 2 moves the screens to it."""
    out = case([("IN", at(D, "23:50:00")), ("OUT", at(D, "00:10:00", days=1))], D, D + timedelta(days=1))
    assert out["rows"] == [("23:50:00", None, None, "missing exit"), (None, "00:10:00", None, "missing entry")]
    assert out["monthly"]["days"] == 2 and out["monthly"]["total_hours"] == 0.0
    same_everywhere(out, ["missing exit", "missing entry"])


def test_h_voided_in_and_corrections():
    out = case([("IN", at(D, "08:00:00")), ("OUT", at(D, "16:00:00"))], corrections=[
        {"action": "void", "event_at": "08:00:00"},
        {"action": "add", "person_id": 1, "direction": "IN", "timestamp": at(D, "08:30:00").isoformat()},
        {"action": "change", "event_at": "16:00:00", "timestamp": at(D, "16:30:00").isoformat()}])
    assert out["events"] == [("08:00:00", "IN", False, True, False), ("08:30:00", "IN", False, False, True),
                             ("16:30:00", "OUT", False, False, False)]  # a voided event is never a repeat
    assert out["rows"] == [("08:30:00", "16:30:00", 8.0, "complete")]
    same_everywhere(out, ["complete"])


def test_x_last_out_before_first_in_is_check():
    out = case([("OUT", at(D, "07:00:00")), ("IN", at(D, "08:00:00"))])
    assert out["rows"] == [("08:00:00", "07:00:00", None, "check")]
    assert out["overview"][0]["check"] == 1 and out["overview"][0]["present"] == 1
    assert out["daily"] == [("check", False, True, None)] and out["csv"] == [["", "YES", "check"]]
    assert out["monthly"]["days_check"] == 1 and out["xlsx_check_days"] == 1
    assert out["monthly"]["days_present"] == 1 and out["monthly"]["total_hours"] == 0.0
    same_everywhere(out, ["check"])


def test_cool_down_does_not_reset_at_midnight():
    out = case([("IN", at(D, "23:58:00")), ("IN", at(D, "00:02:00", days=1))], D, D + timedelta(days=1))
    assert out["decisions"] == ["event", "cool-down"]  # 4 minutes apart: one pass, whatever the date
    assert out["rows"] == [("23:58:00", None, None, "missing exit"), (None, None, None, "absent")]


def test_correction_with_a_time_zone_is_refused_not_a_crash():
    conn = db.connect()
    db.sync_persons(conn, [CODE])
    db.upsert_camera(conn, "entrance", 0, "IN")
    conn.close()
    app = create_app()
    with TestClient(app):
        hr = as_role(app, "hr")
        for ts in ("2026-10-20T08:30:00Z", "2026-10-20T08:30:00+05:00"):
            r = hr.post("/api/attendance/corrections", json={"action": "add", "person_id": 1, "direction": "IN",
                                                             "timestamp": ts, "reason": "x"})
            assert r.status_code == 422 and "without a time zone" in r.text
        assert hr.get("/api/events", params={"from": D, "to": D}).json()["total"] == 0


def test_the_overview_inside_now_counts_people_back_in_after_their_last_out():
    """Case b today (IN, OUT, IN again): the day is complete, but they are in the building now."""
    out = case([("IN", at(TODAY, "00:00:01")), ("OUT", at(TODAY, "00:10:00")), ("IN", at(TODAY, "00:20:00"))],
               TODAY, TODAY)
    assert out["rows"][0][3] == "complete" and out["overview"][0]["inside_now"] == 1
