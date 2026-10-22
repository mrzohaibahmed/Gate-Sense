"""User accounts (admin only: users:manage). Every change needs a reason and is audited in the same transaction."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api import auth
from api.auth import CurrentUser, require_permission
from api.db import parse_ts
from api.permissions import Role

router = APIRouter(prefix="/api/users", tags=["users"], responses=auth.PROTECTED_RESPONSES)
manage = require_permission("users:manage")


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    email: str | None
    role: str
    active: bool
    created_at: datetime
    last_login_at: datetime | None
    must_change_password: bool
    locked_until: datetime | None


def out(row: dict) -> UserOut:
    return UserOut(**{**row, "created_at": parse_ts(row["created_at"]), "last_login_at": parse_ts(row["last_login_at"]),
                      "locked_until": parse_ts(row["locked_until"])})


class Reason(BaseModel):
    reason: str = Field(min_length=1, description="why (goes to the audit log)")

    @field_validator("reason")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a reason is required")
        return v.strip()


class NewUser(Reason):
    username: str
    full_name: str = ""
    email: str | None = None
    role: Role
    password: str = Field(description="temporary password; the user must change it at first login")

    @field_validator("username")
    @classmethod
    def valid_username(cls, v: str) -> str:
        v = auth.normalize_username(v)
        if not auth.USERNAME_RE.match(v):
            raise ValueError("2 to 32 characters: lowercase letters, digits, dot, dash or underscore")
        return v


class RoleChange(Reason):
    role: Role


class PasswordReset(Reason):
    new_password: str = Field(description="temporary password; the user must change it at their next login")


def target(request: Request, user_id: int) -> dict:
    row = auth.db_of(request).user(user_id=user_id)
    if row is None:
        raise HTTPException(404, f"No user with id {user_id}.")
    return row


def keep_an_admin(request: Request, row: dict, what: str) -> None:
    if row["role"] == "admin" and row["active"] and auth.db_of(request).active_admins() <= 1:
        raise HTTPException(409, f"Can't {what} the last active admin.")


@router.get("", response_model=list[UserOut])
def list_users(request: Request, _: CurrentUser = Depends(manage)) -> list[UserOut]:
    return [out(r) for r in auth.db_of(request).users()]


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: NewUser, request: Request, admin: CurrentUser = Depends(manage)) -> UserOut:
    db = auth.db_of(request)
    if problem := auth.password_problem(body.password, body.username):
        raise HTTPException(422, problem.capitalize() + ".")
    if db.user(username=body.username):
        raise HTTPException(409, f"User {body.username!r} already exists.")
    user_id = db.create_user({
        "username": body.username, "full_name": body.full_name.strip(), "email": (body.email or "").strip() or None,
        "role": body.role, "password_hash": auth.hash_password(body.password), "must_change_password": True,
        "created_at": auth.now()}, actor=admin.username, reason=body.reason)
    return out(db.user(user_id=user_id))


@router.patch("/{user_id}/role", response_model=UserOut)
def change_role(user_id: int, body: RoleChange, request: Request, admin: CurrentUser = Depends(manage)) -> UserOut:
    row = target(request, user_id)
    if row["role"] == body.role:
        return out(row)
    if body.role != "admin":
        keep_an_admin(request, row, "change the role of")
    auth.db_of(request).update_user(user_id, {"role": body.role},
                                    audit=(admin.username, "change_user_role", {"role": [row["role"], body.role]},
                                           body.reason))
    return out(target(request, user_id))


@router.post("/{user_id}/deactivate", response_model=UserOut)
def deactivate(user_id: int, body: Reason, request: Request, admin: CurrentUser = Depends(manage)) -> UserOut:
    row = target(request, user_id)
    if row["id"] == admin.id:
        raise HTTPException(409, "You can't deactivate your own account.")
    keep_an_admin(request, row, "deactivate")
    auth.db_of(request).update_user(user_id, {"active": 0},
                                    audit=(admin.username, "deactivate_user", {"active": [row["active"], 0]},
                                           body.reason), end_sessions=True)
    return out(target(request, user_id))


@router.post("/{user_id}/activate", response_model=UserOut)
def activate(user_id: int, body: Reason, request: Request, admin: CurrentUser = Depends(manage)) -> UserOut:
    row = target(request, user_id)
    auth.db_of(request).update_user(user_id, {"active": 1, "failed_logins": 0, "locked_until": None},
                                    audit=(admin.username, "activate_user", {"active": [row["active"], 1]},
                                           body.reason))
    return out(target(request, user_id))


@router.post("/{user_id}/reset-password", response_model=UserOut)
def reset_password(user_id: int, body: PasswordReset, request: Request,
                   admin: CurrentUser = Depends(manage)) -> UserOut:
    """Set a temporary password: the user must change it at next login. Ends their sessions and any lockout."""
    row = target(request, user_id)
    if problem := auth.password_problem(body.new_password, row["username"]):
        raise HTTPException(422, problem.capitalize() + ".")
    auth.db_of(request).update_user(
        user_id, {"password_hash": auth.hash_password(body.new_password), "must_change_password": 1,
                  "failed_logins": 0, "locked_until": None},
        audit=(admin.username, "reset_user_password", {}, body.reason), end_sessions=True)
    return out(target(request, user_id))
