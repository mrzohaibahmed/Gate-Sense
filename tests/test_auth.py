"""API users, roles and login (api/auth.py, api/permissions.py, api/routes/).

A fake clock replaces api.auth.now, so lockout and session expiry are tested without waiting.
"""

import re
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api import auth
from api.main import PUBLIC_PATHS, create_app
from api.workers import WorkerManager
from app import config, db
from app.workers import WorkerManager as AppWorkerManager
from tests.test_api import CAMERAS, FAKE, wait_for

FIRST = "first-admin-password"  # GATESENSE_ADMIN_PASSWORD for the first start
ADMIN = "admin-password-2026"  # what the admin changes it to
PW = "user-password-123"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 22, 9, 0, 0)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def first_password(monkeypatch):
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, FIRST)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(auth, "now", c)
    return c


@pytest.fixture
def app(clock):
    application = create_app()
    with TestClient(application):  # runs the lifespan: tables, first admin
        yield application


def browser(app) -> TestClient:
    """A separate client (own cookie jar) on the same running app."""
    return TestClient(app)


def login(app, username: str, password: str):
    c = browser(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    return c, r


def admin_client(app) -> TestClient:
    """Logged in as admin, first-login password change done."""
    c, r = login(app, "admin", FIRST)
    if r.status_code != 200:
        c, r = login(app, "admin", ADMIN)
        assert r.status_code == 200, r.text
        return c
    assert c.post("/api/auth/password", json={"current_password": FIRST, "new_password": ADMIN}).status_code == 200
    return c


def add_user(app, username: str, role: str, password: str = PW, must_change: bool = False) -> int:
    return app.state.db.create_user({
        "username": username, "full_name": username.title(), "email": None, "role": role,
        "password_hash": auth.hash_password(password), "must_change_password": must_change,
        "created_at": auth.now()}, actor="test", reason="test setup")


def as_role(app, role: str) -> TestClient:
    if role == "admin":
        return admin_client(app)
    add_user(app, f"{role}1", role)
    c, r = login(app, f"{role}1", PW)
    assert r.status_code == 200, r.text
    return c


def audit(app, action: str | None = None) -> list[dict]:
    rows = list(reversed(app.state.db.audit_entries()))
    return [r for r in rows if action is None or r["action"] == action]


# --- First start ---------------------------------------------------------------------


def test_first_start_creates_admin_who_must_change_password(clock, capsys):
    with TestClient(create_app()) as c:
        users = c.app.state.db.users()
        assert [(u["username"], u["role"], u["must_change_password"]) for u in users] == [("admin", "admin", 1)]
        assert users[0]["password_hash"].startswith("$argon2id$")
        created = audit(c.app, "create_user")
        assert [(a["user"], a["target"], a["reason"]) for a in created] == [
            ("system", "user:admin", "first start: no users yet")]
    out = capsys.readouterr().out
    assert "Created user 'admin' with the password from GATESENSE_ADMIN_PASSWORD (environment)" in out
    assert FIRST not in out


def test_later_starts_keep_the_users(clock, monkeypatch):
    with TestClient(create_app()):
        pass
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, "a-different-password")
    with TestClient(create_app()) as c:
        assert len(c.app.state.db.users()) == 1
        assert login(c.app, "admin", FIRST)[1].status_code == 200  # not reset to the new variable


def test_no_users_and_no_password_refuses_to_start(clock, monkeypatch):
    monkeypatch.delenv(config.DASHBOARD_PASSWORD_ENV)
    with pytest.raises(RuntimeError, match="No API users yet, and GATESENSE_ADMIN_PASSWORD is not set"):
        with TestClient(create_app()):
            pass


# --- Login, me, password change, logout ----------------------------------------------


def test_login_sets_a_strict_httponly_session_cookie(app):
    c, r = login(app, "admin", FIRST)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{config.SESSION_COOKIE}=")
    for part in ("HttpOnly", "Max-Age=28800", "Path=/"):
        assert part in cookie
    assert "samesite=strict" in cookie.lower() and "secure" not in cookie.lower()
    token = cookie.split(";")[0].split("=", 1)[1]
    with closing(db.connect()) as conn:  # only the token's hash is stored
        stored = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
    assert stored == [auth.hash_token(token)] and token not in stored

    me = c.get("/api/auth/me").json()
    assert (me["username"], me["role"], me["must_change_password"]) == ("admin", "admin", True)
    assert "users:manage" in me["permissions"]
    assert [(a["user"], a["action"]) for a in audit(app, "login")] == [("admin", "login")]


def test_password_must_be_changed_first(app):
    c, _ = login(app, "admin", FIRST)
    r = c.get("/api/users")
    assert r.status_code == 403 and "Change your password first" in r.json()["detail"]
    assert c.post("/api/auth/password", json={"current_password": "wrong-one!!", "new_password": ADMIN}
                  ).status_code == 400
    assert audit(app, "password_change_failed")[0]["user"] == "admin"
    assert c.post("/api/auth/password", json={"current_password": FIRST, "new_password": "short"}).status_code == 422
    assert c.post("/api/auth/password", json={"current_password": FIRST, "new_password": FIRST}).status_code == 422
    assert c.post("/api/auth/password", json={"current_password": FIRST, "new_password": ADMIN}).status_code == 200
    assert c.get("/api/users").status_code == 200
    assert c.get("/api/auth/me").json()["must_change_password"] is False
    assert login(app, "admin", FIRST)[1].status_code == 401
    assert login(app, "admin", ADMIN)[1].status_code == 200
    assert audit(app, "change_password")[0]["user"] == "admin"


def test_password_change_ends_other_sessions(app):
    add_user(app, "ann", "viewer")
    phone, _ = login(app, "ann", PW)
    laptop, _ = login(app, "ann", PW)
    assert laptop.post("/api/auth/password", json={"current_password": PW, "new_password": "new-password-9"}
                       ).status_code == 200
    assert laptop.get("/api/auth/me").status_code == 200
    assert phone.get("/api/auth/me").status_code == 401


def test_logout(app):
    c, _ = login(app, "admin", FIRST)
    r = c.post("/api/auth/logout")
    assert r.status_code == 200 and "Max-Age=0" in r.headers["set-cookie"]
    assert c.get("/api/auth/me").status_code == 401
    assert app.state.db.session_count(app.state.db.user(username="admin")["id"]) == 0
    assert [(a["user"], a["target"]) for a in audit(app, "logout")] == [("admin", "user:admin")]


def test_not_logged_in(app):
    r = browser(app).get("/api/auth/me")
    assert r.status_code == 401 and r.json()["detail"] == "Not logged in."
    stranger = browser(app)
    stranger.cookies.set(config.SESSION_COOKIE, "made-up-token")
    r = stranger.get("/api/auth/me")
    assert r.status_code == 401 and "session ended" in r.json()["detail"]


def test_usernames_are_case_insensitive(app):
    add_user(app, "bilal", "viewer")
    assert login(app, "  Bilal ", PW)[1].status_code == 200


# --- Roles -----------------------------------------------------------------------------

# What each role may reach. "allowed" = the request gets past the permission check.
ENDPOINTS = {
    "me": ("GET", "/api/auth/me", None),
    "cameras": ("GET", "/api/cameras", None),
    "camera start": ("POST", "/api/cameras/99999/start", None),  # past the check: 404
    "users": ("GET", "/api/users", None),
    "create user": ("POST", "/api/users", {"username": "made-by-{role}", "role": "viewer",
                                            "password": "temporary-pw-1", "reason": "new starter"}),
}
EXPECTED = {
    "admin": {"me", "cameras", "camera start", "users", "create user"},
    "hr": {"me", "cameras"},
    "viewer": {"me", "cameras"},
    "guard": {"me", "cameras", "camera start"},
}


@pytest.mark.parametrize("role", list(EXPECTED))
def test_role_reaches_allowed_and_not_forbidden_endpoints(app, role):
    c = as_role(app, role)
    username = c.get("/api/auth/me").json()["username"]
    for name, (method, path, body) in ENDPOINTS.items():
        json = {k: v.format(role=role) for k, v in body.items()} if body else None
        r = c.request(method, path, json=json)
        if name in EXPECTED[role]:
            assert r.status_code not in (401, 403), (role, name, r.status_code, r.text)
        else:
            assert r.status_code == 403, (role, name, r.status_code)
            assert f"Your role ({role}) does not allow this" in r.json()["detail"]
            denied = audit(app, "permission_denied")[-1]
            assert (denied["user"], denied["target"]) == (username, f"{method} {path}")
            assert denied["reason"].startswith(f"role {role} lacks ")


def test_expected_matrix_matches_the_permission_table():
    """The table above is written out by hand, so a change in api/permissions.py has to be made here too."""
    from api.permissions import ROLE_PERMISSIONS, ROLES

    assert set(EXPECTED) == set(ROLES)
    needs = {"me": "account:self", "cameras": "cameras:read", "camera start": "cameras:control",
             "users": "users:manage", "create user": "users:manage"}
    for role, perms in ROLE_PERMISSIONS.items():
        assert {name for name, p in needs.items() if p in perms} == EXPECTED[role]


def test_every_endpoint_except_login_and_health_needs_a_login(app):
    paths = app.openapi()["paths"]
    assert len(paths) >= 10
    public_seen = set()
    for path, methods in paths.items():
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in methods:
            if path in PUBLIC_PATHS:
                public_seen.add(path)
                continue
            r = browser(app).request(method.upper(), url, json={})
            assert r.status_code == 401, (method, path, r.status_code)
    assert public_seen == PUBLIC_PATHS


@pytest.fixture
def camera_app(clock, tmp_path):
    from app import camera_settings

    camera_settings.save([{**c, "type": "webcam" if isinstance(c["source"], int) else "file", "mirror": False}
                          for c in CAMERAS])  # the camera list the API shows = the fake workers' cameras
    run, logs, live_dir = tmp_path / "run", tmp_path / "logs", tmp_path / "live"
    inner = AppWorkerManager(None, script=FAKE, run_dir=run, log_dir=logs, live_dir=live_dir,
                             extra_args=("--no-window", "--run-dir", str(run), "--frames", "--live-dir", str(live_dir)))
    application = create_app(workers=WorkerManager(inner))
    try:
        with TestClient(application):
            yield application
    finally:
        for proc in inner._procs.values():
            proc.kill()


def test_guard_starts_and_stops_a_camera_viewer_cannot(camera_app):
    guard, viewer = as_role(camera_app, "guard"), as_role(camera_app, "viewer")
    c = {cam["name"]: cam for cam in viewer.get("/api/cameras").json()["items"]}["c"]["id"]
    assert viewer.post(f"/api/cameras/{c}/start").status_code == 403
    r = guard.post(f"/api/cameras/{c}/start")
    assert r.status_code == 200 and r.json()["held_by"] == "api"
    wait_for(lambda: guard.get(f"/api/cameras/{c}").json()["worker"]["state"] == "running")
    busy = guard.post(f"/api/cameras/{c}/start")
    assert busy.status_code == 409 and "already running" in busy.json()["detail"]
    assert guard.post(f"/api/cameras/{c}/stop").json() == {"camera": "c", "result": "stopped cleanly"}
    assert [(a["user"], a["target"]) for a in audit(camera_app, "start_camera")] == [("guard1", "camera:c")]
    assert [(a["user"], a["target"]) for a in audit(camera_app, "stop_camera")] == [("guard1", "camera:c")]


# --- Lockout --------------------------------------------------------------------------


def test_five_failed_logins_lock_the_account_for_15_minutes(app, clock):
    add_user(app, "alice", "viewer")
    for i in range(4):
        r = login(app, "alice", "wrong-password")[1]
        assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password."
    r = login(app, "alice", "wrong-password")[1]
    assert r.status_code == 423 and "locked for 15 minutes" in r.json()["detail"]
    r = login(app, "alice", PW)[1]  # even the right password, while locked
    assert r.status_code == 423 and "Try again after 09:15" in r.json()["detail"]
    clock.advance(minutes=14, seconds=59)
    assert login(app, "alice", PW)[1].status_code == 423
    clock.advance(seconds=1)
    assert login(app, "alice", PW)[1].status_code == 200
    assert app.state.db.user(username="alice")["failed_logins"] == 0

    failed = audit(app, "login_failed")
    assert [a["user"] for a in failed] == ["alice"] * 5
    assert failed[-1]["reason"] == "wrong password (5 in a row): locked until 09:15"
    assert [a["user"] for a in audit(app, "login_locked")] == ["alice", "alice"]
    assert [a["user"] for a in audit(app, "login")] == ["alice"]


def test_a_successful_login_resets_the_count(app):
    add_user(app, "omar", "viewer")
    for _ in range(4):
        login(app, "omar", "wrong-password")
    assert login(app, "omar", PW)[1].status_code == 200
    for _ in range(4):
        assert login(app, "omar", "wrong-password")[1].status_code == 401  # not locked: the count started again


def test_unknown_user_gets_the_same_answer_and_is_audited(app):
    r = login(app, "nobody", "whatever-123")[1]
    assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password."
    row = audit(app, "login_failed")[-1]
    assert (row["user"], row["target"], row["reason"]) == ("nobody", "user:nobody", "unknown username")


# --- Session expiry --------------------------------------------------------------------


def test_session_expires_after_8_hours_without_activity(app, clock):
    c, _ = login(app, "admin", FIRST)
    clock.advance(hours=8, seconds=1)
    r = c.get("/api/auth/me")
    assert r.status_code == 401 and "Session expired" in r.json()["detail"]
    assert app.state.db.session_count(app.state.db.user(username="admin")["id"]) == 0


def test_activity_keeps_the_session_alive(app, clock):
    c, _ = login(app, "admin", FIRST)
    clock.advance(hours=7)
    r = c.get("/api/auth/me")
    assert r.status_code == 200 and "Max-Age=28800" in r.headers["set-cookie"]  # refreshed cookie
    assert r.json()["session_expires"] == "2026-10-23T00:00:00"  # 8 h after this request (16:00)
    clock.advance(seconds=30)
    assert "set-cookie" not in c.get("/api/auth/me").headers  # refreshed at most once a minute
    clock.advance(hours=7)
    assert c.get("/api/auth/me").status_code == 200  # 14 h after login, still active
    clock.advance(hours=8, seconds=1)
    assert c.get("/api/auth/me").status_code == 401


# --- Admin: users ------------------------------------------------------------------------


def test_admin_creates_users_with_a_temporary_password(app):
    a = admin_client(app)
    body = {"username": "Sara.K", "full_name": "Sara Khan", "email": "sara@example.com", "role": "hr",
            "password": "temporary-pw-1", "reason": "new HR officer"}
    r = a.post("/api/users", json=body)
    assert r.status_code == 201
    user = r.json()
    assert (user["username"], user["role"], user["must_change_password"], user["active"]) == ("sara.k", "hr", True, True)
    assert "password_hash" not in user and "password" not in user
    assert a.post("/api/users", json=body).status_code == 409
    assert a.post("/api/users", json={**body, "username": "x2", "role": "boss"}).status_code == 422
    assert a.post("/api/users", json={**body, "username": "x3", "password": "short"}).status_code == 422
    assert a.post("/api/users", json={**body, "username": "x4", "reason": "  "}).status_code == 422
    assert a.post("/api/users", json={**body, "username": "bad name!"}).status_code == 422
    row = audit(app, "create_user")[-1]
    assert (row["user"], row["target"], row["reason"]) == ("admin", "user:sara.k", "new HR officer")
    assert "temporary-pw-1" not in row["details"] and "argon2" not in row["details"]
    assert [u["username"] for u in a.get("/api/users").json()["items"]] == ["admin", "sara.k"]

    c, r = login(app, "sara.k", "temporary-pw-1")
    assert r.json()["must_change_password"] is True


def test_admin_changes_a_role(app):
    a = admin_client(app)
    uid = add_user(app, "zain", "viewer")
    z, _ = login(app, "zain", PW)
    assert z.get("/api/users").status_code == 403
    r = a.patch(f"/api/users/{uid}/role", json={"role": "admin", "reason": "IT lead"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert z.get("/api/users").status_code == 200  # applies to the open session at once
    row = audit(app, "change_user_role")[-1]
    assert row["user"] == "admin" and '"role": ["viewer", "admin"]' in row["details"]


def test_the_last_admin_stays(app):
    a = admin_client(app)
    me = a.get("/api/auth/me").json()["id"]
    r = a.patch(f"/api/users/{me}/role", json={"role": "viewer", "reason": "oops"})
    assert r.status_code == 409 and "last active admin" in r.json()["detail"]
    assert a.post(f"/api/users/{me}/deactivate", json={"reason": "leaving"}).status_code == 409


def test_admin_deactivates_and_activates_a_user(app):
    a = admin_client(app)
    uid = add_user(app, "imran", "guard")
    g, _ = login(app, "imran", PW)
    r = a.post(f"/api/users/{uid}/deactivate", json={"reason": "left the company"})
    assert r.status_code == 200 and r.json()["active"] is False
    assert g.get("/api/auth/me").status_code == 401  # their session ended
    r = login(app, "imran", PW)[1]
    assert r.status_code == 403 and r.json()["detail"] == "This account is deactivated."
    assert a.post(f"/api/users/{uid}/activate", json={"reason": "rehired"}).json()["active"] is True
    assert login(app, "imran", PW)[1].status_code == 200
    assert [a_["reason"] for a_ in audit(app) if a_["action"] in ("deactivate_user", "activate_user")] == [
        "left the company", "rehired"]
    me = a.get("/api/auth/me").json()["id"]
    assert a.post(f"/api/users/{me}/deactivate", json={"reason": "x"}).status_code == 409
    assert a.post("/api/users/999/deactivate", json={"reason": "x"}).status_code == 404


def test_admin_resets_a_password(app, clock):
    a = admin_client(app)
    uid = add_user(app, "hina", "hr")
    h, _ = login(app, "hina", PW)
    for _ in range(5):
        login(app, "hina", "wrong-password")  # locked out
    r = a.post(f"/api/users/{uid}/reset-password", json={"new_password": "temporary-pw-2", "reason": "forgot it"})
    assert r.status_code == 200 and r.json()["must_change_password"] is True and r.json()["locked_until"] is None
    assert h.get("/api/auth/me").status_code == 401  # sessions ended
    c, r = login(app, "hina", "temporary-pw-2")
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert c.get("/api/cameras").status_code == 403  # must change it first
    row = audit(app, "reset_user_password")[-1]
    assert (row["user"], row["target"], row["reason"]) == ("admin", "user:hina", "forgot it")
    assert "temporary-pw-2" not in row["details"]
