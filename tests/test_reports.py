import io
from datetime import date, datetime

import openpyxl
import pytest

from app import db, reports


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "attendance.db")
    db.sync_persons(c, ["ali", "waqas"])
    ids = db.person_ids(c)
    cin = db.upsert_camera(c, "entrance", 0, "IN")
    cout = db.upsert_camera(c, "exit", 1, "OUT")
    # waqas: two full days; ali: one day without an exit
    for d in (1, 2):
        db.add_event(c, ids["waqas"], cin, datetime(2026, 10, d, 9, 0), 0.8, None)
        db.add_event(c, ids["waqas"], cout, datetime(2026, 10, d, 17, 0), 0.8, None)
    db.add_event(c, ids["ali"], cin, datetime(2026, 10, 2, 9, 30), 0.7, None)
    yield c
    c.close()


def test_daily_report(conn):
    df = reports.daily_report(conn, date(2026, 10, 2))
    assert list(df["Employee code"]) == ["ali", "waqas"]
    ali = df.iloc[0]
    assert ali["First IN"] == "09:30:00" and ali["Last OUT"] == "" and ali["Missing exit"] == "YES"
    assert df.iloc[1]["Worked hours"] == 8.0


def test_monthly_report(conn):
    summary, detail = reports.monthly_report(conn, 2026, 10, until=date(2026, 10, 5))
    waqas = summary.set_index("Employee code").loc["waqas"]
    assert waqas["Days present"] == 2 and waqas["Total hours"] == 16.0 and waqas["Avg hours/day"] == 8.0
    ali = summary.set_index("Employee code").loc["ali"]
    assert ali["Days present"] == 0 and ali["Days missing exit"] == 1
    assert len(detail) == 3  # absent days are left out of the detail
    assert set(detail["Date"]) == {"2026-10-01", "2026-10-02"}


def test_month_days_stop_at_until():
    assert reports.month_days(2026, 10, until=date(2026, 10, 3)) == [date(2026, 10, d) for d in (1, 2, 3)]
    assert len(reports.month_days(2026, 2, until=date(2027, 1, 1))) == 28


def test_excel_and_csv_export(conn):
    summary, detail = reports.monthly_report(conn, 2026, 10, until=date(2026, 10, 5))
    wb = openpyxl.load_workbook(io.BytesIO(reports.to_excel({"Summary": summary, "Daily": detail})))
    assert wb.sheetnames == ["Summary", "Daily"]
    assert wb["Summary"]["A1"].value == "Employee code"
    assert wb["Daily"].max_row == len(detail) + 1
    csv = reports.to_csv(detail).decode("utf-8-sig")
    assert csv.splitlines()[0].startswith("Date,Employee code,Name")
