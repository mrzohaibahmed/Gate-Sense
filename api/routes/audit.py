"""The audit log: every correction, change to people, users, cameras, sites and settings, and every login."""

import json
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from api import auth
from api.auth import CurrentUser, require_permission
from api.db import parse_ts
from api.paging import ListParams, Page, global_list_params
from api.schemas import day_range

router = APIRouter(prefix="/api/audit", tags=["audit"], responses=auth.PROTECTED_RESPONSES)


class AuditEntry(BaseModel):
    id: int
    timestamp: datetime
    user: str
    action: str
    target: str
    details: dict
    reason: str


@router.get("", response_model=Page[AuditEntry])
def list_audit(request: Request, params: ListParams = Depends(global_list_params),
               date_from: date | None = Query(None, alias="from", description="first day (default: no limit)"),
               date_to: date | None = Query(None, alias="to", description="last day, inclusive"),
               user: str | None = Query(None, description="exact user name"),
               action: str | None = Query(None, description="exact action, e.g. edit_event or login_failed"),
               _: CurrentUser = Depends(require_permission("audit:read"))) -> dict:
    """Newest first by default. The audit log isn't per site, so there is no site_id filter."""
    start = end = None
    if date_from or date_to:
        try:
            first, last = day_range(date_from, date_to, max_days=100000)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        start, end = datetime.combine(first, datetime.min.time()), datetime.combine(last + timedelta(days=1),
                                                                                     datetime.min.time())
    rows, total = auth.db_of(request).audit_page(params, start, end, user, action)
    items = [AuditEntry(id=r["id"], timestamp=parse_ts(r["timestamp"]), user=r["user"], action=r["action"],
                        target=r["target"], details=json.loads(r["details"] or "{}"), reason=r["reason"])
             for r in rows]
    return {"items": items, "total": total, "page": params.page, "page_size": params.page_size}
