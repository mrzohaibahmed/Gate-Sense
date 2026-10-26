"""The roster: which shift (or OFF) each person works on each date, one entry per person per date.

GET /api/roster is the grid (people x dates) for the Roster screen; PUT /api/roster saves cells (a shift, OFF, or
removed), all in one transaction, each change audited (set_roster / clear_roster) with the one reason given. The days
are computed from the raw events whenever they are read (app/shifts.py), so a change shows at once on every screen
and report, and the raw events never change.

Import: GET /api/roster/import/template gives the grid as a file (the current entries filled in), POST
/api/roster/import/preview says what importing a file would do, cell by cell, with every error, changing nothing;
POST /api/roster/import does it (the whole file or nothing). The file is the raw request body, its name in `filename`.
Admin and HR change the roster (roster:manage); the viewer role reads it (roster:read); guards don't see it.
"""

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, list_params, paginate
from api.routes.people_import import FILE_BODY, XLSX
from api.schemas import Reason
from app import config
from app.people_import import ImportFileError

router = APIRouter(prefix="/api/roster", tags=["roster"], dependencies=[Depends(auth.shifts_on)],
                   responses={**auth.PROTECTED_RESPONSES, 409: {"description": "shifts are switched off"}})
read = require_permission("roster:read")
manage = require_permission("roster:manage")
PREVIEW_LIMIT = 1000  # changes and errors listed in a preview (the counts are always complete)


class RosterCell(BaseModel):
    date: date
    shift_id: int | None = Field(description="the shift; None = OFF")


class RosterRow(BaseModel):
    person_id: int
    employee_code: str
    name: str
    department: str | None
    site_id: int | None
    active: bool
    cells: list[RosterCell] = Field(description="the dates with an entry (a date left out has none: unscheduled)")


class RosterEntry(BaseModel):
    person_id: int
    date: date
    shift_id: int | None = Field(None, description="the shift; None = OFF (unless clear)")
    clear: bool = Field(False, description="remove the entry: the date becomes unscheduled")


class RosterUpdate(Reason):
    entries: list[RosterEntry] = Field(min_length=1, max_length=config.ROSTER_MAX_ENTRIES)


class RosterChanged(BaseModel):
    changed: int = Field(description="entries added or changed")
    removed: int = Field(description="entries removed")


class RosterImportChange(BaseModel):
    line: int = Field(description="row number in the file (1 = the heading row)")
    column: str = Field(description="the date heading as written")
    employee_code: str
    name: str
    date: date
    old: str | None = Field(description="the entry now: a shift's name, OFF, or None (no entry)")
    new: str = Field(description="a shift's name or OFF")


class RosterImportError(BaseModel):
    line: int
    column: str = Field(description="the date heading; empty for a problem with the whole row")
    employee_code: str
    message: str


class RosterImportCounts(BaseModel):
    set: int = Field(description="cells that add or change an entry")
    unchanged: int
    error: int


class RosterImportPreview(BaseModel):
    file: str
    dates: list[date]
    people: int = Field(description="rows with a known, active person")
    ignored_columns: list[str] = Field(description="headings that are neither the code, the name nor a date")
    counts: RosterImportCounts
    changes: list[RosterImportChange] = Field(description=f"the first {PREVIEW_LIMIT} changes")
    errors: list[RosterImportError] = Field(description=f"the first {PREVIEW_LIMIT} errors")


class RosterImportResult(BaseModel):
    file: str
    changed: int
    unchanged: int


def roster_range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """from/to, inclusive; neither = this week (Monday to Sunday); only from = a week from it."""
    monday = date.today() - timedelta(days=date.today().weekday())
    first = date_from or (date_to - timedelta(days=6) if date_to else monday)
    last = date_to or first + timedelta(days=6)
    if last < first:
        raise HTTPException(422, "'to' is before 'from'.")
    if (last - first).days >= config.ROSTER_MAX_DAYS:
        raise HTTPException(422, f"At most {config.ROSTER_MAX_DAYS} days at a time.")
    return first, last


RANGE = {
    "date_from": Query(None, alias="from", description="first date (default: this week's Monday)"),
    "date_to": Query(None, alias="to", description="last date, inclusive (default: a week from `from`)"),
}


@router.get("", response_model=Page[RosterRow])
def get_roster(request: Request, params: ListParams = Depends(list_params),
               date_from: date | None = RANGE["date_from"], date_to: date | None = RANGE["date_to"],
               department: str | None = Query(None, description="exact department name"),
               _: CurrentUser = Depends(read)) -> dict:
    """The roster grid: every active person (and inactive ones with entries in the range), each with their entries
    from `from` to `to` (at most ROSTER_MAX_DAYS). site_id = the person's site. Default sort: name."""
    first, last = roster_range(date_from, date_to)
    db = auth.db_of(request)
    cells: dict[int, list[RosterCell]] = {}
    for r in db.roster(first, last):
        cells.setdefault(r["person_id"], []).append(RosterCell(date=r["date"], shift_id=r["shift_id"]))
    rows = [{"person_id": p["id"], "employee_code": p["employee_code"], "name": p["name"],
             "department": p["department"], "site_id": p["site_id"], "active": bool(p["active"]),
             "cells": cells.get(p["id"], [])}
            for p in db.people() if p["active"] or p["id"] in cells]
    rows = [r for r in rows if (params.site_id is None or r["site_id"] == params.site_id)
            and (not department or (r["department"] or "") == department)]
    return paginate(rows, params, ("name", "employee_code", "department"), ("name", "employee_code", "department"),
                    "name", tiebreak="employee_code")


@router.put("", response_model=RosterChanged,
            responses={404: {"description": "no such person"}, 422: {"description": "no such shift, or a cell twice"}})
def save_roster(body: RosterUpdate, request: Request, user: CurrentUser = Depends(manage)) -> RosterChanged:
    """Set cells: each entry a shift, OFF (shift_id null), or removed (clear). All in one transaction; each change is
    audited with the reason. Entries that are already so change nothing."""
    db = auth.db_of(request)
    people = {p["id"] for p in db.people()}
    shifts = {s["shift"].id for s in db.shifts()}
    seen = set()
    sets, clears = [], []
    for e in body.entries:
        if (e.person_id, e.date) in seen:
            raise HTTPException(422, f"Person {e.person_id} on {e.date} is in the list twice.")
        seen.add((e.person_id, e.date))
        if e.person_id not in people:
            raise HTTPException(404, f"No person with id {e.person_id}.")
        if e.clear:
            clears.append((e.person_id, e.date))
        elif e.shift_id is not None and e.shift_id not in shifts:
            raise HTTPException(422, f"No shift with id {e.shift_id}.")
        else:
            sets.append((e.person_id, e.date, e.shift_id))
    changed, removed = db.set_roster(sets, clears, user.username, body.reason)
    return RosterChanged(changed=changed, removed=removed)


@router.get("/import/template", response_class=Response, responses={200: {"content": {XLSX: {}, "text/csv": {}},
                                                                          "description": "the roster as a file"}})
def roster_template(request: Request, format: Literal["xlsx", "csv"] = Query("xlsx"),  # noqa: A002
                    date_from: date | None = RANGE["date_from"], date_to: date | None = RANGE["date_to"],
                    site_id: int | None = Query(None, description="people of this site only"),
                    department: str | None = Query(None, description="exact department name"),
                    _: CurrentUser = Depends(manage)) -> Response:
    """The roster of the dates as a file to fill in and import: Employee code, Name, one column per date (each cell a
    shift's name, OFF or blank). The Excel file also lists the shifts."""
    first, last = roster_range(date_from, date_to)
    body = auth.db_of(request).roster_template(first, last, format, site_id, department or None)
    return Response(body, media_type=XLSX if format == "xlsx" else "text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="gatesense-roster-{first}_{last}.{format}"'})


def preview_out(result, filename: str) -> RosterImportPreview:
    changes = [c for c in result.cells if c.action == "set"]
    return RosterImportPreview(
        file=filename, dates=result.dates, people=result.people, ignored_columns=result.ignored_columns,
        counts=RosterImportCounts(set=len(changes), unchanged=result.count("unchanged"), error=len(result.errors)),
        changes=[RosterImportChange(line=c.line, column=c.column, employee_code=c.employee_code, name=c.name,
                                    date=c.date, old=c.old, new=c.new) for c in changes[:PREVIEW_LIMIT]],
        errors=[RosterImportError(line=e.line, column=e.column, employee_code=e.employee_code, message=e.message)
                for e in result.errors[:PREVIEW_LIMIT]])


@router.post("/import/preview", response_model=RosterImportPreview, openapi_extra=FILE_BODY)
async def preview_roster_import(request: Request,
                                filename: str = Query("", description="the file's name, e.g. roster.xlsx"),
                                _: CurrentUser = Depends(manage)) -> RosterImportPreview:
    """What importing the file would do: the cells that add or change an entry, the unchanged ones, and every error
    (with its line and column). Nothing is changed."""
    data = await request.body()
    try:
        result = await run_in_threadpool(auth.db_of(request).plan_roster_import, data, filename)
    except ImportFileError as e:
        raise HTTPException(422, str(e)[0].upper() + str(e)[1:] + ".") from None
    return preview_out(result, filename)


@router.post("/import", response_model=RosterImportResult, openapi_extra=FILE_BODY,
             responses={422: {"description": "the file can't be read, or a cell has an error: nothing imported"}})
async def import_roster(request: Request, filename: str = Query("", description="the file's name"),
                        reason: str = Query(..., min_length=1, description="why (goes to the audit log)"),
                        user: CurrentUser = Depends(manage)) -> RosterImportResult:
    """Write every change in the file, all in one go: refused (422) while any cell has an error. Blank cells leave
    their day as it is. Each change is audited (set_roster), plus one import_roster row for the file."""
    if not reason.strip():
        raise HTTPException(422, "A reason is required.")
    data = await request.body()
    try:
        result = await run_in_threadpool(auth.db_of(request).import_roster, data, filename, user.username,
                                         reason.strip())
    except ImportFileError as e:
        raise HTTPException(422, str(e)[0].upper() + str(e)[1:] + ".") from None
    except ValueError as e:
        raise HTTPException(422, f"Nothing was imported. Fix these first: {e}.") from None
    return RosterImportResult(file=filename, changed=result.count("set"), unchanged=result.count("unchanged"))
