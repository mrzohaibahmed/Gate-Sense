"""Reports: the daily register and the per-person totals (monthly summary) for a date range, as JSON for the
preview and as CSV or Excel downloads. Every export is written to the audit log.

A working day is a roster day (app/reports.py range_rows -> app.db.roster_days, the same rule as /api/attendance,
the overview and scripts/today.py): one per roster row (a shift or OFF) plus unscheduled dates with passes, the first
IN in the shift's IN window to the last OUT in its OUT window, with late / early leave / overtime minutes. A night
shift is dated the day it starts.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.schemas import Status, day_range
from app import reports

router = APIRouter(prefix="/api/reports", tags=["reports"], responses=auth.PROTECTED_RESPONSES)
read = require_permission("reports:read")
Kind = Literal["daily", "monthly"]


class DailyRow(BaseModel):
    date: Annotated[date, Field(description="the roster date (a night shift: the day it starts)")]
    employee_code: str
    name: str
    department: str | None
    shift: str | None = Field(description="the shift's name; None for an off day or unscheduled")
    shift_start: datetime | None
    shift_end: datetime | None
    first_in: datetime | None
    last_out: datetime | None
    worked_hours: float | None = Field(description="last OUT minus first IN, in hours, when both count")
    late_minutes: int | None
    early_leave_minutes: int | None
    overtime_minutes: int | None
    status: Status
    open: bool = Field(description="the day is still running (its OUT window hasn't closed)")


class PersonTotal(BaseModel):
    employee_code: str
    name: str
    department: str | None
    days: int = Field(description="roster days in the range (shifts, OFF days and unscheduled dates with passes)")
    days_scheduled: int = Field(description="days rostered on a shift")
    days_present: int = Field(description="came in for the shift (on time, late, early leave, missing OUT)")
    days_on_time: int
    days_late: int
    late_minutes: int = Field(description="minutes late, added up")
    days_early_leave: int
    early_leave_minutes: int
    overtime_minutes: int
    days_missing_in: int
    days_missing_out: int
    days_check: int = Field(description="days with passes but none where they count")
    days_absent: int
    days_not_in_yet: int = Field(description="today's shifts whose IN window is still open")
    days_off: int
    days_unscheduled: int
    total_hours: float
    avg_hours: float | None = Field(description="total hours / days with hours worked")


class DailyReport(BaseModel):
    first: date
    last: date
    rows: list[DailyRow]


class MonthlyReport(BaseModel):
    first: date
    last: date
    people: list[PersonTotal]


def month_start(today: date) -> date:
    return today.replace(day=1)


def resolve(kind: str, date_from: date | None, date_to: date | None) -> tuple[date, date]:
    if kind == "monthly" and date_from is None and date_to is None:
        date_from = month_start(date.today())
    try:
        return day_range(date_from, date_to)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


def rows_for(request: Request, first: date, last: date, department: str | None, site_id: int | None) -> list[dict]:
    with auth.db_of(request).conn() as conn:
        return reports.range_rows(conn, first, last, department or None, site_id)


FILTERS = {
    "date_from": Query(None, alias="from", description="first day (default: today; this month for monthly)"),
    "date_to": Query(None, alias="to", description="last day, inclusive"),
    "department": Query(None, description="exact department name"),
    "site_id": Query(None, description="people of this site only"),
}


@router.get("/daily", response_model=DailyReport)
def daily_report(request: Request, date_from: date | None = FILTERS["date_from"],
                 date_to: date | None = FILTERS["date_to"], department: str | None = FILTERS["department"],
                 site_id: int | None = FILTERS["site_id"], _: CurrentUser = Depends(read)) -> dict:
    """The daily register: each roster day, the shift, first IN, last OUT, hours worked, late / early leave /
    overtime minutes and the status."""
    first, last = resolve("daily", date_from, date_to)
    return {"first": first, "last": last, "rows": rows_for(request, first, last, department, site_id)}


@router.get("/monthly", response_model=MonthlyReport)
def monthly_report(request: Request, date_from: date | None = FILTERS["date_from"],
                   date_to: date | None = FILTERS["date_to"], department: str | None = FILTERS["department"],
                   site_id: int | None = FILTERS["site_id"], _: CurrentUser = Depends(read)) -> dict:
    """Per person over the range: scheduled and present days, days per status, late / early leave / overtime
    minutes, total hours (added up from the daily register)."""
    first, last = resolve("monthly", date_from, date_to)
    return {"first": first, "last": last,
            "people": reports.person_totals(rows_for(request, first, last, department, site_id))}


@router.get("/{kind}/export", response_class=Response, responses={200: {"content": {
    "text/csv": {}, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}},
    "description": "the report as a file"}})
def export_report(kind: Kind, request: Request, format: Literal["csv", "xlsx"] = Query("xlsx"),  # noqa: A002
                  date_from: date | None = FILTERS["date_from"], date_to: date | None = FILTERS["date_to"],
                  department: str | None = FILTERS["department"], site_id: int | None = FILTERS["site_id"],
                  user: CurrentUser = Depends(read)) -> Response:
    """Download a report. CSV: the daily register, or the per-person totals. Excel: one sheet per report (the monthly
    export also has the daily detail), header styled, first row frozen. Audited."""
    first, last = resolve(kind, date_from, date_to)
    rows = rows_for(request, first, last, department, site_id)
    label = "Daily register" if kind == "daily" else "Monthly summary"
    title = f"GateSense {label.lower()} {first} to {last}" + (f", {department}" if department else "")
    if kind == "daily":
        sheets = [(label, reports.DAILY_COLUMNS_RANGE, rows)]
    else:
        sheets = [("Summary", reports.TOTAL_COLUMNS, reports.person_totals(rows)),
                  ("Daily detail", reports.DAILY_COLUMNS_RANGE, rows)]
    if format == "csv":
        body, media = reports.rows_csv(sheets[0][1], sheets[0][2]), "text/csv; charset=utf-8"
    else:
        body = reports.rows_xlsx(sheets, title)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    name = f"gatesense-{kind}-{first}_{last}{'-' + department.replace(' ', '_') if department else ''}.{format}"
    auth.db_of(request).audit(user.username, "export_report", f"report:{kind}",
                              {"from": str(first), "to": str(last), "department": department, "site_id": site_id,
                               "format": format, "rows": len(sheets[0][2])}, f"{label} exported")
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})
