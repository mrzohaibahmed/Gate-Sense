"""Reports: the daily register and the per-person totals (monthly summary) for a date range, as JSON for the
preview and as CSV or Excel downloads. Every export is written to the audit log.

A working day is the calendar date, first IN to last OUT (app/reports.py range_rows -> app.db.day_summaries, the
same rule as /api/attendance and the overview); there are no shifts or late rules yet. The exports' Status column
says "inside" for today's days without an OUT yet, like the screens.
"""

from datetime import date, datetime
from typing import Literal

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
    date: date
    employee_code: str
    name: str
    department: str | None
    first_in: datetime | None
    last_out: datetime | None
    worked_hours: float | None = Field(description="last OUT minus first IN, in hours (complete days only)")
    missing_out: bool = Field(description="came in but no OUT that day (not for today: still inside)")
    check: bool = Field(description="the last OUT is earlier than the first IN: needs a look")
    status: Status


class PersonTotal(BaseModel):
    employee_code: str
    name: str
    department: str | None
    days: int = Field(description="days in the range")
    days_present: int = Field(description="days with an IN (complete, missing exit or check)")
    days_missing_exit: int = Field(description="days with an IN but no OUT (today not counted while still inside)")
    days_missing_entry: int
    days_check: int = Field(description="days whose last OUT is earlier than the first IN")
    days_absent: int
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
    """The daily register: each person each day, first IN, last OUT, hours worked, missing OUT, check."""
    first, last = resolve("daily", date_from, date_to)
    return {"first": first, "last": last, "rows": rows_for(request, first, last, department, site_id)}


@router.get("/monthly", response_model=MonthlyReport)
def monthly_report(request: Request, date_from: date | None = FILTERS["date_from"],
                   date_to: date | None = FILTERS["date_to"], department: str | None = FILTERS["department"],
                   site_id: int | None = FILTERS["site_id"], _: CurrentUser = Depends(read)) -> dict:
    """Per person over the range: days present, missing exits, days to check, total hours (added up from the daily
    register)."""
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
