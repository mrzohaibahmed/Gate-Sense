"""People > Import: add and update people from an Excel (.xlsx) or CSV file (app/people_import.py).

POST /api/people/import/preview checks the file and says what importing it would do, changing nothing;
POST /api/people/import does it (the whole file or nothing: refused while any row has an error);
GET /api/people/import/template gives an empty file with the headings. The file is the raw request body, its name in
`filename` (used for the type and the audit log). Registered before the people router, so "import" is never taken
for a person id.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from api import auth
from api.auth import CurrentUser, require_permission
from app.people_import import ImportFileError, ImportPlan, template

router = APIRouter(prefix="/api/people/import", tags=["people"], responses=auth.PROTECTED_RESPONSES)
manage = require_permission("people:manage")
FILE_BODY = {"requestBody": {"required": True, "content": {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {"schema": {"type": "string",
                                                                                     "format": "binary"}},
    "text/csv": {"schema": {"type": "string", "format": "binary"}}}}}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ImportRowOut(BaseModel):
    line: int = Field(description="row number in the file (1 = the heading row)")
    employee_code: str
    name: str
    department: str | None
    shift: str | None
    site: str | None
    action: Literal["new", "update", "unchanged", "error"]
    person_id: int | None
    changes: dict[str, list[str | None]] = Field(description='for an update: {"department": [old, new], ...}')
    errors: list[str]
    notes: list[str]


class ImportCounts(BaseModel):
    new: int
    update: int
    unchanged: int
    error: int


class ImportPreview(BaseModel):
    file: str
    columns: list[str] = Field(description="the fields found: employee_code, name, department, shift, site")
    ignored_columns: list[str] = Field(description="headings that aren't one of those (not imported)")
    counts: ImportCounts
    rows: list[ImportRowOut]


class ImportResult(BaseModel):
    file: str
    added: int
    updated: int
    unchanged: int


def out(result: ImportPlan, filename: str) -> ImportPreview:
    return ImportPreview(
        file=filename, columns=result.columns, ignored_columns=result.ignored_columns,
        counts=ImportCounts(**{a: result.count(a) for a in ("new", "update", "unchanged", "error")}),
        rows=[ImportRowOut(line=r.line, employee_code=r.employee_code, name=r.name, department=r.department,
                           shift=r.shift, site=r.site, action=r.action, person_id=r.person_id, changes=r.changes,
                           errors=r.errors, notes=r.notes) for r in result.rows])


@router.get("/template", response_class=Response, responses={200: {"content": {XLSX: {}, "text/csv": {}},
                                                                    "description": "a file to fill in"}})
def import_template(format: Literal["xlsx", "csv"] = Query("xlsx"),  # noqa: A002
                    _: CurrentUser = Depends(manage)) -> Response:
    """An empty file with the headings (Employee code, Name, Department, Shift, Site) and one example row."""
    return Response(template(format), media_type=XLSX if format == "xlsx" else "text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="gatesense-people.{format}"'})


@router.post("/preview", response_model=ImportPreview, openapi_extra=FILE_BODY)
async def preview_import(request: Request, filename: str = Query("", description="the file's name, e.g. staff.xlsx"),
                         _: CurrentUser = Depends(manage)) -> ImportPreview:
    """What importing the file would do, row by row: new people, changes to existing ones (only the columns the
    file has), unchanged rows, and rows with errors. Nothing is changed."""
    data = await request.body()
    try:
        result = await run_in_threadpool(auth.db_of(request).plan_import, data, filename)
    except ImportFileError as e:
        raise HTTPException(422, str(e).capitalize() + ".") from None
    return out(result, filename)


@router.post("", response_model=ImportResult, openapi_extra=FILE_BODY,
             responses={422: {"description": "the file can't be read, or a row has an error: nothing imported"}})
async def import_people(request: Request, filename: str = Query("", description="the file's name"),
                        reason: str = Query(..., min_length=1, description="why (goes to the audit log)"),
                        user: CurrentUser = Depends(manage)) -> ImportResult:
    """Add the new people and update the changed ones, all in one go: refused (422) while any row has an error.
    People not in the file are left alone; inactive ones are updated but not reactivated. Each person added or changed
    is audited (add_person, edit_person), plus one import_people row for the file. The people have no photos yet:
    enroll their faces next."""
    if not reason.strip():
        raise HTTPException(422, "A reason is required.")
    data = await request.body()
    try:
        result = await run_in_threadpool(auth.db_of(request).import_people, data, filename, user.username,
                                         reason.strip())
    except ImportFileError as e:
        raise HTTPException(422, str(e).capitalize() + ".") from None
    except ValueError as e:
        raise HTTPException(422, f"Nothing was imported. Fix these rows first: {e}.") from None
    return ImportResult(file=filename, added=result.count("new"), updated=result.count("update"),
                        unchanged=result.count("unchanged"))
