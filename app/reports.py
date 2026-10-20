"""Daily and monthly attendance reports as pandas tables, with CSV and Excel export."""

import calendar
import io
import sqlite3
from datetime import date

import pandas as pd

from app import db
from app.attendance import MISSING_ENTRY, MISSING_EXIT, PRESENT

DAILY_COLUMNS = ["Date", "Employee code", "Name", "Department", "First IN", "Last OUT", "Worked hours",
                 "Missing exit", "Status"]


def _hms(dt) -> str:
    return dt.strftime("%H:%M:%S") if dt else ""


def daily_report(conn: sqlite3.Connection, day: date) -> pd.DataFrame:
    """One row per person for `day` (refreshes daily_attendance first)."""
    people = {r["employee_code"]: r for r in db.list_persons(conn)}
    rows = []
    for s in db.refresh_daily(conn, day):
        p = people.get(s.person)
        rows.append({
            "Date": day.isoformat(),
            "Employee code": s.person,
            "Name": p["name"] if p else s.person,
            "Department": (p["department"] if p else None) or "",
            "First IN": _hms(s.first_in),
            "Last OUT": _hms(s.last_out),
            "Worked hours": s.worked_hours,
            "Missing exit": "YES" if s.status == MISSING_EXIT else "",
            "Status": s.status,
        })
    df = pd.DataFrame(rows, columns=DAILY_COLUMNS)
    return df.sort_values("Name", kind="stable").reset_index(drop=True)


def month_days(year: int, month: int, until: date | None = None) -> list[date]:
    """Days of the month, stopping at `until` (default today) so future days aren't reported."""
    until = until or date.today()
    last = calendar.monthrange(year, month)[1]
    return [d for d in (date(year, month, n) for n in range(1, last + 1)) if d <= until]


def monthly_report(conn: sqlite3.Connection, year: int, month: int,
                   until: date | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(summary per person, daily detail without absent days) for a month."""
    days = month_days(year, month, until)
    daily = [daily_report(conn, d) for d in days]
    detail = pd.concat(daily, ignore_index=True) if daily else pd.DataFrame(columns=DAILY_COLUMNS)
    detail = detail[detail["Status"] != "absent"].reset_index(drop=True)

    people = [(r["employee_code"], r["name"], r["department"] or "") for r in db.list_persons(conn)
              if r["active"] or r["employee_code"] in set(detail["Employee code"])]
    rows = []
    for code, name, dept in people:
        mine = detail[detail["Employee code"] == code]
        hours = pd.to_numeric(mine["Worked hours"], errors="coerce").dropna()
        present = int((mine["Status"] == PRESENT).sum())
        rows.append({
            "Employee code": code,
            "Name": name,
            "Department": dept,
            "Days present": present,
            "Days missing exit": int((mine["Status"] == MISSING_EXIT).sum()),
            "Days missing entry": int((mine["Status"] == MISSING_ENTRY).sum()),
            "Total hours": round(float(hours.sum()), 2),
            "Avg hours/day": round(float(hours.mean()), 2) if len(hours) else None,
        })
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values("Name", kind="stable").reset_index(drop=True)
    return summary, detail


def to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")  # BOM so Excel opens it as UTF-8


def to_excel(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name[:31], index=False)
            ws = writer.sheets[name[:31]]
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(width + 2, 40)
    return buf.getvalue()
