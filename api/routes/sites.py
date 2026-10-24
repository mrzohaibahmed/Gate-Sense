"""Sites: every camera and every person belongs to one. Changing them is admin only; the list is readable by every
role, for the site filter. (A site has no time zone of its own: a day is local midnight to midnight on this PC.)"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, global_list_params, paginate
from api.schemas import Reason

router = APIRouter(prefix="/api/sites", tags=["sites"], responses=auth.PROTECTED_RESPONSES)
manage = require_permission("sites:manage")


class Site(BaseModel):
    id: int
    name: str
    cameras: int
    people: int


class SiteIn(Reason):
    name: str = Field(min_length=1, max_length=80)


class SitePatch(Reason):
    name: str | None = Field(None, min_length=1, max_length=80)


def get_or_404(request: Request, site_id: int) -> dict:
    site = auth.db_of(request).site(site_id)
    if site is None:
        raise HTTPException(404, f"No site with id {site_id}.")
    return site


def name_free(request: Request, name: str, except_id: int | None = None) -> None:
    if any(s["name"].lower() == name.lower() and s["id"] != except_id for s in auth.db_of(request).sites()):
        raise HTTPException(409, f"There is already a site called {name!r}.")


@router.get("", response_model=Page[Site])
def list_sites(request: Request, params: ListParams = Depends(global_list_params),
               _: CurrentUser = Depends(require_permission("sites:read"))) -> dict:
    return paginate(auth.db_of(request).sites(), params, ("name",), ("id", "name"), "id")


@router.get("/{site_id}", response_model=Site)
def get_site(site_id: int, request: Request, _: CurrentUser = Depends(require_permission("sites:read"))) -> dict:
    return get_or_404(request, site_id)


@router.post("", response_model=Site, status_code=201)
def create_site(body: SiteIn, request: Request, admin: CurrentUser = Depends(manage)) -> dict:
    name_free(request, body.name.strip())
    site_id = auth.db_of(request).create_site(body.name.strip(), admin.username, body.reason)
    return get_or_404(request, site_id)


@router.patch("/{site_id}", response_model=Site)
def update_site(site_id: int, body: SitePatch, request: Request, admin: CurrentUser = Depends(manage)) -> dict:
    site = get_or_404(request, site_id)
    changes = {"name": body.name.strip()} if body.name is not None else {}
    if "name" in changes:
        name_free(request, changes["name"], site_id)
    auth.db_of(request).update_site(site_id, changes, admin.username, body.reason)
    return get_or_404(request, site["id"])


@router.delete("/{site_id}", status_code=204, responses={409: {"description": "the site still has cameras or "
                                                                              "people, or it is the last one"}})
def delete_site(site_id: int, request: Request, reason: str = Query(..., min_length=1, description="why"),
                admin: CurrentUser = Depends(manage)) -> None:
    """Only an empty site can be deleted (move its cameras and people first), and never the last one."""
    site = get_or_404(request, site_id)
    if not reason.strip():
        raise HTTPException(422, "A reason is required.")
    if site["cameras"] or site["people"]:
        raise HTTPException(409, f"{site['name']} still has {site['cameras']} camera(s) and {site['people']} "
                                 "person(s); move them to another site first.")
    if len(auth.db_of(request).sites()) == 1:
        raise HTTPException(409, "The last site can't be deleted.")
    auth.db_of(request).delete_site(site_id, admin.username, reason.strip())
