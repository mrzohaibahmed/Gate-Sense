"""Login, logout, the current user and changing your own password."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from api import auth
from api.auth import CurrentUser, require_permission

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/login", response_model=CurrentUser, responses={
    401: {"description": "Wrong username or password"}, 403: {"description": "Account deactivated"},
    423: {"description": "Account locked after too many failed logins"}})
def login(body: LoginBody, request: Request, response: Response) -> CurrentUser:
    """Log in: sets the session cookie (httpOnly, SameSite=Strict). Public: the only endpoint without a login."""
    token, _ = auth.login(auth.db_of(request), body.username, body.password, request)
    auth.set_session_cookie(response, token)
    return auth.session_user(request, Response(), token)


@router.post("/logout", responses=auth.PROTECTED_RESPONSES)
def logout(request: Request, response: Response,
           user: CurrentUser = Depends(require_permission("account:self", allow_password_change=True))) -> dict:
    auth.db_of(request).end_session(
        request.state.session_id,
        (user.username, "logout", f"user:{user.username}", auth.client(request), "logged out"))
    response.delete_cookie(auth.config.SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=auth.config.SESSION_COOKIE_SECURE)
    return {"detail": "Logged out."}


@router.get("/me", response_model=CurrentUser, responses=auth.PROTECTED_RESPONSES)
def me(user: CurrentUser = Depends(require_permission("account:self", allow_password_change=True))) -> CurrentUser:
    return user


@router.post("/password", responses=auth.PROTECTED_RESPONSES)
def change_password(body: PasswordBody, request: Request,
                    user: CurrentUser = Depends(require_permission("account:self", allow_password_change=True))
                    ) -> dict:
    """Change your own password (required after the first login and after an admin reset). Ends your other
    sessions."""
    db = auth.db_of(request)
    row = db.user(user_id=user.id)
    if not auth.verify_password(row["password_hash"], body.current_password):
        db.audit(user.username, "password_change_failed", f"user:{user.username}", auth.client(request),
                 "wrong current password")
        raise HTTPException(400, "The current password is wrong.")
    if problem := auth.password_problem(body.new_password, user.username):
        raise HTTPException(422, problem.capitalize() + ".")
    if body.new_password == body.current_password:
        raise HTTPException(422, "The new password must be different from the current one.")
    db.update_user(user.id, {"password_hash": auth.hash_password(body.new_password), "must_change_password": 0},
                   audit=(user.username, "change_password", {}, "changed by the user"),
                   end_sessions=True, keep_session=request.state.session_id)
    return {"detail": "Password changed."}
