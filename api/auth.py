"""Login, sessions and the permission check that guards every API endpoint.

- Passwords: argon2 (argon2-cffi), rehashed on login when the parameters change.
- Login sets an httpOnly, SameSite=Strict cookie holding a random token; only its
  SHA-256 is stored (sessions.token_hash). A session ends SESSION_HOURS after the
  last request: each request extends it (at most every SESSION_REFRESH_SECONDS)
  and re-sends the cookie with a fresh Max-Age.
- LOGIN_MAX_FAILURES wrong passwords in a row lock the account for
  LOGIN_LOCK_MINUTES (even the right password is refused meanwhile).
- Logins, failed logins, logouts, lockouts and permission denials go to the
  audit log with the username (for a failed login: the name that was typed).
- First start with no users: "admin" is created with GATESENSE_ADMIN_PASSWORD
  (environment or .env) and must change it at first login. Until a user
  changes a password marked must_change_password, only account:self endpoints
  that allow it (me, logout, change password) work.

``now()`` is the clock for everything here; tests replace it.
"""

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import APIKeyCookie
from pydantic import BaseModel

from api.db import Database, parse_ts
from api.permissions import ROLE_PERMISSIONS, allowed
from app import config
from app.env import admin_password_source

hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash("no such user: checked anyway so unknown names take as long as real ones")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
cookie_scheme = APIKeyCookie(name=config.SESSION_COOKIE, auto_error=False, description="Set by POST /api/auth/login")


# For routers' `responses=`, so /docs lists the answers every protected endpoint can give.
PROTECTED_RESPONSES = {
    401: {"description": "Not logged in, or the session expired (log in with POST /api/auth/login)"},
    403: {"description": "Your role lacks the permission, or you must change your password first"},
}


def now() -> datetime:
    return datetime.now()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str, username: str = "") -> str | None:
    """Why a new password is refused, or None."""
    if len(password) < config.PASSWORD_MIN_LENGTH:
        return f"the password must be at least {config.PASSWORD_MIN_LENGTH} characters"
    if username and password.lower() == username.lower():
        return "the password can't be the username"
    return None


def normalize_username(username: str) -> str:
    return username.strip().lower()


def client(request: Request) -> dict:
    return {"ip": request.client.host if request.client else None,
            "user_agent": (request.headers.get("user-agent") or "")[:300] or None}


def db_of(request: Request) -> Database:
    return request.app.state.db


class CurrentUser(BaseModel):
    id: int
    username: str
    full_name: str
    email: str | None
    role: str
    permissions: list[str]
    must_change_password: bool
    session_expires: datetime


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(config.SESSION_COOKIE, token, max_age=config.SESSION_HOURS * 3600, path="/",
                        httponly=True, samesite="strict", secure=config.SESSION_COOKIE_SECURE)


def clear_cookie_header() -> dict:
    return {"set-cookie": f"{config.SESSION_COOKIE}=\"\"; Max-Age=0; Path=/; HttpOnly; SameSite=strict"}


def not_logged_in(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers=clear_cookie_header())


# --- First start ------------------------------------------------------------------


def bootstrap_admin(db: Database) -> str | None:
    """Create the first admin if there are no users; returns a message for the log (never the password)."""
    if db.count_users():
        return None
    source = admin_password_source()
    password = os.environ.get(config.DASHBOARD_PASSWORD_ENV)
    if source is None or not password:
        raise RuntimeError(f"No API users yet, and {config.DASHBOARD_PASSWORD_ENV} is not set (environment or .env): "
                           "it is needed once, to create the first admin account.")
    db.create_user({"username": config.API_FIRST_ADMIN, "full_name": "Administrator", "email": None, "role": "admin",
                    "password_hash": hash_password(password), "must_change_password": True, "created_at": now()},
                   actor="system", reason="first start: no users yet")
    return (f"Created user {config.API_FIRST_ADMIN!r} with the password from {config.DASHBOARD_PASSWORD_ENV} "
            f"({source}); it must be changed at first login.")


# --- Login / logout -----------------------------------------------------------------


def login(db: Database, username: str, password: str, request: Request) -> tuple[str, dict]:
    """Check the password and open a session; returns (cookie token, user). Raises HTTPException otherwise."""
    name = normalize_username(username)
    info = client(request)
    t = now()
    user = db.user(username=name)

    def fail(status: int, detail: str, action: str, reason: str, **extra) -> HTTPException:
        db.audit(name or "(empty)", action, f"user:{name}", {**info, **extra}, reason)
        return HTTPException(status, detail)

    locked_until = parse_ts(user["locked_until"]) if user else None
    if locked_until and locked_until > t:
        verify_password(_DUMMY_HASH, password)
        raise fail(423, f"This account is locked after {config.LOGIN_MAX_FAILURES} failed logins. Try again after "
                        f"{locked_until:%H:%M}.", "login_locked", f"login refused: account locked until {locked_until:%H:%M}")
    ok = verify_password(user["password_hash"] if user else _DUMMY_HASH, password)
    if user is None:
        raise fail(401, "Wrong username or password.", "login_failed", "unknown username")
    if not ok:
        failures = user["failed_logins"] + 1
        if failures >= config.LOGIN_MAX_FAILURES:
            until = t + timedelta(minutes=config.LOGIN_LOCK_MINUTES)
            db.update_user(user["id"], {"failed_logins": 0, "locked_until": until})
            raise fail(423, f"Wrong password. The account is now locked for {config.LOGIN_LOCK_MINUTES} minutes.",
                       "login_failed", f"wrong password ({failures} in a row): locked until {until:%H:%M}",
                       failed_logins=failures)
        db.update_user(user["id"], {"failed_logins": failures})
        raise fail(401, "Wrong username or password.", "login_failed", f"wrong password ({failures} in a row)",
                   failed_logins=failures)
    if not user["active"]:
        raise fail(403, "This account is deactivated.", "login_failed", "account deactivated")

    token = secrets.token_urlsafe(32)
    changes = {"failed_logins": 0, "locked_until": None, "last_login_at": t}
    if hasher.check_needs_rehash(user["password_hash"]):
        changes["password_hash"] = hash_password(password)
    db.create_session(user["id"], hash_token(token), t, t + timedelta(hours=config.SESSION_HOURS),
                      info["ip"], info["user_agent"], changes,
                      (user["username"], "login", f"user:{user['username']}", info, "password accepted"))
    return token, user


# --- The session behind each request -------------------------------------------------


def session_user(request: Request, response: Response, token: str | None = Depends(cookie_scheme)) -> CurrentUser:
    """The logged-in user; extends the session. 401 without a valid, unexpired session."""
    if not token:
        raise not_logged_in("Not logged in.")
    db = db_of(request)
    s = db.session(hash_token(token))
    if s is None:
        raise not_logged_in("Not logged in (the session ended).")
    t = now()
    expires = parse_ts(s["expires_at"])
    if expires <= t:
        db.end_session(s["id"])
        raise not_logged_in(f"Session expired after {config.SESSION_HOURS} hours without activity. Log in again.")
    if not s["active"]:
        db.end_session(s["id"])
        raise not_logged_in("This account is deactivated.")
    fresh = t + timedelta(hours=config.SESSION_HOURS)
    if (fresh - expires).total_seconds() >= config.SESSION_REFRESH_SECONDS:
        db.extend_session(s["id"], fresh)
        set_session_cookie(response, token)
        expires = fresh
    request.state.session_id = s["id"]
    return CurrentUser(id=s["user_id"], username=s["username"], full_name=s["full_name"], email=s["email"],
                       role=s["role"], permissions=sorted(ROLE_PERMISSIONS.get(s["role"], ())),
                       must_change_password=bool(s["must_change_password"]), session_expires=expires)


def require_permission(permission: str, allow_password_change: bool = False):
    """Dependency for an endpoint: the user must be logged in and their role must have `permission`.

    While the user must change their password, only endpoints with allow_password_change=True work.
    Denials are written to the audit log.
    """

    def check(request: Request, user: CurrentUser = Depends(session_user)) -> CurrentUser:
        if user.must_change_password and not allow_password_change:
            raise HTTPException(403, "Change your password first (POST /api/auth/password).")
        if not allowed(user.role, permission):
            db_of(request).audit(user.username, "permission_denied", f"{request.method} {request.url.path}",
                                 {"permission": permission, "role": user.role, **client(request)},
                                 f"role {user.role} lacks {permission}")
            raise HTTPException(403, f"Your role ({user.role}) does not allow this ({permission}).")
        return user

    check.permission = permission  # lets tests confirm that every endpoint has a permission check
    return check
