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


# --- Any date range, for the API's reports (daily register, per-person totals) ---------------------------------
# Both build on db.day_summaries (attendance.summarize_person: first IN to last OUT of the calendar day), the same
# rule as the Today page and /api/attendance: shift windows would be added there, in one place.

DAILY_COLUMNS_RANGE = [  # (header, key, kind) for exports
    ("Date", "date", "date"), ("Employee code", "employee_code", "text"), ("Name", "name", "text"),
    ("Department", "department", "text"), ("First IN", "first_in", "time"), ("Last OUT", "last_out", "time"),
    ("Hours worked", "worked_hours", "hours"), ("Missing OUT", "missing_out", "yesno"), ("Status", "status", "text"),
]
TOTAL_COLUMNS = [
    ("Employee code", "employee_code", "text"), ("Name", "name", "text"), ("Department", "department", "text"),
    ("Days", "days", "int"), ("Days present", "days_present", "int"), ("Missing exits", "days_missing_exit", "int"),
    ("Missing entries", "days_missing_entry", "int"), ("Days absent", "days_absent", "int"),
    ("Total hours", "total_hours", "hours"), ("Average hours per worked day", "avg_hours", "hours"),
]


def range_rows(conn: sqlite3.Connection, first: date, last: date, department: str | None = None,
               site_id: int | None = None) -> list[dict]:
    """One row per person per day from `first` to `last` (everyone active, plus anyone with events), by date then
    name. Read-only."""
    from datetime import timedelta

    people = {r["employee_code"]: dict(r) for r in db.list_persons(conn)}
    rows = []
    day = first
    while day <= last:
        summaries, _ = db.day_summaries(conn, day)
        for s in summaries:
            p = people.get(s.person, {})
            if department and (p.get("department") or "") != department:
                continue
            if site_id is not None and p.get("site_id") != site_id:
                continue
            rows.append({"date": day, "employee_code": s.person, "name": p.get("name", s.person),
                         "department": p.get("department"), "first_in": s.first_in, "last_out": s.last_out,
                         "worked_hours": s.worked_hours, "missing_out": s.status == MISSING_EXIT, "status": s.status})
        day += timedelta(days=1)
    return sorted(rows, key=lambda r: (r["date"], r["name"].lower(), r["employee_code"]))


def person_totals(rows: list[dict]) -> list[dict]:
    """Per person, from the daily rows only (so the totals always add up to them): days in the range, days present
    (came in), missing exits and entries, absent days, total hours, and the average over the days with hours."""
    totals: dict[str, dict] = {}
    for r in rows:
        t = totals.setdefault(r["employee_code"], {
            "employee_code": r["employee_code"], "name": r["name"], "department": r["department"], "days": 0,
            "days_present": 0, "days_missing_exit": 0, "days_missing_entry": 0, "days_absent": 0,
            "total_hours": 0.0, "avg_hours": None, "_worked_days": 0})
        t["days"] += 1
        t["days_present"] += r["status"] in (PRESENT, MISSING_EXIT)
        t["days_missing_exit"] += r["status"] == MISSING_EXIT
        t["days_missing_entry"] += r["status"] == MISSING_ENTRY
        t["days_absent"] += r["status"] == "absent"
        if r["worked_hours"] is not None:
            t["total_hours"] += r["worked_hours"]
            t["_worked_days"] += 1
    out = []
    for t in totals.values():
        worked = t.pop("_worked_days")
        t["total_hours"] = round(t["total_hours"], 2)
        t["avg_hours"] = round(t["total_hours"] / worked, 2) if worked else None
        out.append(t)
    return sorted(out, key=lambda t: (t["name"].lower(), t["employee_code"]))


def _cell(value, kind: str):
    if value is None:
        return ""
    if kind == "time":
        return value.strftime("%H:%M:%S")
    if kind == "date":
        return value.isoformat()
    if kind == "yesno":
        return "YES" if value else ""
    return value


def rows_csv(columns: list[tuple[str, str, str]], rows: list[dict]) -> bytes:
    """CSV with a BOM so Excel opens it as UTF-8."""
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([h for h, _, _ in columns])
    for r in rows:
        writer.writerow([_cell(r.get(k), kind) for _, k, kind in columns])
    return buf.getvalue().encode("utf-8-sig")


def rows_xlsx(sheets: list[tuple[str, list[tuple[str, str, str]], list[dict]]], title: str) -> bytes:
    """One sheet per (name, columns, rows): bold white header on the GateSense teal, first row frozen, filters,
    real dates, times (hh:mm) and numbers, column widths fitted. `title` goes in the document properties."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title = title
    wb.properties.creator = "GateSense"
    header_font, header_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0E7C66")
    for name, columns, rows in sheets:
        ws = wb.create_sheet(name[:31])
        ws.append([h for h, _, _ in columns])
        for cell in ws[1]:
            cell.font, cell.fill = header_font, header_fill
            cell.alignment = Alignment(vertical="center")
        for r in rows:
            values = []
            for _, key, kind in columns:
                v = r.get(key)
                values.append(v.time() if kind == "time" and v is not None else
                              ("YES" if v else "") if kind == "yesno" else ("" if v is None else v))
            ws.append(values)
        formats = {"date": "yyyy-mm-dd", "time": "hh:mm", "hours": "0.00", "int": "0"}
        for i, (header, _, kind) in enumerate(columns, start=1):
            letter = ws.cell(row=1, column=i).column_letter
            if kind in formats:
                for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    cell.number_format = formats[kind]
            width = max([len(header)] + [len(str(_cell(r.get(columns[i - 1][1]), kind))) for r in rows[:500]])
            ws.column_dimensions[letter].width = min(width + 2, 40)
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
