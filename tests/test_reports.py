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


def test_daily_register(conn):
    rows = reports.range_rows(conn, date(2026, 10, 2), date(2026, 10, 2))
    assert [r["employee_code"] for r in rows] == ["ali", "waqas"]
    ali, waqas = rows
    assert ali["first_in"] == datetime(2026, 10, 2, 9, 30) and ali["last_out"] is None and ali["missing_out"]
    assert waqas["worked_hours"] == 8.0 and not waqas["missing_out"]


def test_person_totals(conn):
    rows = reports.range_rows(conn, date(2026, 10, 1), date(2026, 10, 5))
    ali, waqas = reports.person_totals(rows)
    assert (waqas["days"], waqas["days_present"], waqas["days_absent"]) == (5, 2, 3)
    assert waqas["total_hours"] == 16.0 and waqas["avg_hours"] == 8.0
    assert ali["days_present"] == 1 and ali["days_missing_exit"] == 1 and ali["avg_hours"] is None


def test_excel_and_csv_export(conn):
    rows = reports.range_rows(conn, date(2026, 10, 1), date(2026, 10, 5))
    totals = reports.person_totals(rows)
    wb = openpyxl.load_workbook(io.BytesIO(reports.rows_xlsx(
        [("Summary", reports.TOTAL_COLUMNS, totals), ("Daily detail", reports.DAILY_COLUMNS_RANGE, rows)], "t")))
    assert wb.sheetnames == ["Summary", "Daily detail"]
    assert wb["Summary"]["A1"].value == "Employee code" and wb["Summary"].freeze_panes == "A2"
    assert wb["Daily detail"].max_row == len(rows) + 1
    csv = reports.rows_csv(reports.DAILY_COLUMNS_RANGE, rows).decode("utf-8-sig")
    assert csv.splitlines()[0].startswith("Date,Employee code,Name")
    assert "2026-10-02,ali,ali,,09:30:00,,,YES,,missing exit" in csv.splitlines()  # Missing OUT, Check, Status
