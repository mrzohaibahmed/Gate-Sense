"""Paging, sorting and search for every list endpoint.

Query parameters: page (from 1), page_size (up to MAX_PAGE_SIZE), sort (a field
name, "-" in front for descending, e.g. "-date"), q (case-insensitive text search)
and, where rows belong to a site, site_id. The answer is a Page: items, total,
page, page_size.

Small or computed lists (people, cameras, daily attendance) are paged in
memory with ``paginate``; big tables (events, audit log) in SQL with
``order_by`` and ``limit_offset``.
"""

from dataclasses import dataclass
from typing import Generic, TypeVar

from fastapi import HTTPException, Query
from pydantic import BaseModel

T = TypeVar("T")
MAX_PAGE_SIZE = 500


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int  # rows matching the filters, on all pages
    page: int
    page_size: int


@dataclass
class ListParams:
    page: int
    page_size: int
    sort: str | None
    q: str | None
    site_id: int | None = None


SORT_HELP = 'field to sort by; prefix with "-" for descending, e.g. "-date"'


def list_params(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
                sort: str | None = Query(None, description=SORT_HELP),
                q: str | None = Query(None, description="text search (case-insensitive)"),
                site_id: int | None = Query(None, description="only rows of this site")) -> ListParams:
    """For lists whose rows belong to a site."""
    return ListParams(page, page_size, sort, (q or "").strip() or None, site_id)


def global_list_params(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
                       sort: str | None = Query(None, description=SORT_HELP),
                       q: str | None = Query(None, description="text search (case-insensitive)")) -> ListParams:
    """For lists that aren't per site (users, sites, audit log)."""
    return ListParams(page, page_size, sort, (q or "").strip() or None)


def sort_field(params: ListParams, allowed, default: str) -> tuple[str, bool]:
    """(field, descending) from params.sort; 422 for a field not in `allowed`."""
    raw = params.sort or default
    desc = raw.startswith("-")
    name = raw.lstrip("-+")
    if name not in allowed:
        raise HTTPException(422, f"can't sort by {name!r}; use one of: {', '.join(sorted(allowed))}")
    return name, desc


def paginate(rows: list[dict], params: ListParams, search: tuple[str, ...], sortable: tuple[str, ...],
             default_sort: str, tiebreak: str | None = None) -> dict:
    """Filter by q over `search` fields, sort (None last, then `tiebreak`), cut out the page."""
    if params.q:
        needle = params.q.lower()
        rows = [r for r in rows if any(needle in str(r.get(f) or "").lower() for f in search)]
    field, desc = sort_field(params, sortable, default_sort)
    if tiebreak:
        rows = sorted(rows, key=lambda r: (r.get(tiebreak) is None, r.get(tiebreak) or ""))
    present = [r for r in rows if r.get(field) is not None]
    missing = [r for r in rows if r.get(field) is None]
    rows = sorted(present, key=lambda r: r[field], reverse=desc) + missing
    start = (params.page - 1) * params.page_size
    return {"items": rows[start:start + params.page_size], "total": len(rows), "page": params.page,
            "page_size": params.page_size}


def order_by(params: ListParams, columns: dict[str, str], default_sort: str, tiebreak: str) -> str:
    """SQL ORDER BY from params.sort; `columns` maps API field names to SQL (only these are ever used)."""
    field, desc = sort_field(params, columns, default_sort)
    return f"ORDER BY {columns[field]} {'DESC' if desc else 'ASC'}, {tiebreak}"


def limit_offset(params: ListParams) -> tuple[str, tuple[int, int]]:
    return "LIMIT ? OFFSET ?", (params.page_size, (params.page - 1) * params.page_size)
