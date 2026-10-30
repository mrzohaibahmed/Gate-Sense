"""Phase 8: the web build served at / (SPA fallback), security headers, the request size limit, the login rate
limit, the secure cookie on HTTPS, the HTTPS check, the start/stop of the one app, and the nightly backup and
snapshot retention."""

import json
import os
import time
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api import auth, maintenance, run, web
from api.main import create_app
from app import config, db
from tests.test_data_api import admin, as_role, first_password, items, seed  # noqa: F401  (first_password: fixture)


@pytest.fixture
def dist(tmp_path, monkeypatch):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>GateSense</title><div id=root></div>")
    (d / "assets" / "index-abc123.js").write_text("console.log('app')")
    (d / "favicon.svg").write_text("<svg/>")
    monkeypatch.setattr(config, "WEB_DIST", d)
    return d


@pytest.fixture
def app(dist):
    application = create_app()
    with TestClient(application):
        application.state.seed = seed()
        yield application


# --- One app: the dashboard at / ----------------------------------------------------------------------


def test_the_dashboard_is_served_with_spa_fallback(app):
    c = TestClient(app)
    for path in ("/", "/attendance", "/people/3", "/settings?tab=users"):
        r = c.get(path)
        assert r.status_code == 200 and "<div id=root>" in r.text and r.headers["cache-control"] == "no-cache", path
    js = c.get("/assets/index-abc123.js")
    assert js.status_code == 200 and "immutable" in js.headers["cache-control"]
    assert c.get("/favicon.svg").text == "<svg/>"
    assert c.get("/assets/missing.js").status_code == 404
    api404 = c.get("/api/no-such-thing")
    assert api404.status_code == 404 and api404.json() == {"detail": "Not Found"}
    assert c.get("/api/health").json()["status"] == "ok"
    assert c.get("/docs").status_code == 200 and "swagger" in c.get("/docs").text.lower()
    assert c.get("/..%2F..%2Fapp%2Fconfig.py").status_code == 404  # never a file outside the build


@pytest.mark.parametrize("path", [
    "//attacker/share/x", r"\\attacker\share\x", "/attacker/share/x", "C:/Windows/win.ini", r"C:\Windows\win.ini",
    "c:", "../../app/config.py", "assets/../../app/config.py", r"a\b", "x\0y",
])
def test_unsafe_web_paths_are_refused_on_the_text(path):
    assert not web.safe_web_path(path)


@pytest.mark.parametrize("path", ["favicon.svg", "people/3", "attendance", "settings", "assets/index-abc123.js"])
def test_ordinary_web_paths_are_fine(path):
    assert web.safe_web_path(path)


@pytest.mark.parametrize("url", [
    "/%2F%2Fattacker%2Fshare%2Fx",  # //attacker/share/x: a UNC path once joined to the build folder on Windows
    "/%5C%5Cattacker%5Cshare%5Cx",  # the same with backslashes
    "/C:%5CWindows%5Cwin.ini", "/C:/Windows/win.ini", "/..%2F..%2Fapp%2Fconfig.py",
])
def test_unsafe_web_paths_never_reach_the_file_system(app, monkeypatch, url):
    """No login is needed for the dashboard's files, so the check must come before resolve(): on Windows, resolving
    a UNC path already connects to that host and offers this account's credentials."""
    import pathlib

    resolved = []
    real = pathlib.Path.resolve

    def spy(self, *args, **kwargs):
        resolved.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", spy)
    r = TestClient(app).get(url)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert not [p for p in resolved if "attacker" in p or "win.ini" in p or "config.py" in p]


def test_without_a_build_the_root_says_how_to_make_one(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WEB_DIST", tmp_path / "nothing")
    with TestClient(create_app()) as c:
        r = c.get("/")
        assert r.status_code == 503 and "npm run build" in r.text
        assert c.get("/api/health").status_code == 200


def test_build_status(tmp_path):
    web_dir, dist_dir = tmp_path / "web", tmp_path / "web" / "dist"
    (web_dir / "src").mkdir(parents=True)
    (web_dir / "src" / "main.tsx").write_text("x")
    assert web.build_status(web_dir, dist_dir) == "missing"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("x")
    old = time.time() - 100
    os.utime(web_dir / "src" / "main.tsx", (old, old))
    assert web.build_status(web_dir, dist_dir) == "ok"
    (web_dir / "src" / "main.tsx").write_text("changed")
    os.utime(web_dir / "src" / "main.tsx", (time.time() + 10, time.time() + 10))
    assert web.build_status(web_dir, dist_dir) == "stale"


# --- Security headers and limits ------------------------------------------------------------------------


def test_security_headers(app):
    c = TestClient(app)
    for path in ("/", "/api/health", "/api/no-such-thing"):
        h = c.get(path).headers
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff", path
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self';" in csp
        assert "strict-transport-security" not in h  # not on plain HTTP
    assert "cdn.jsdelivr.net" in c.get("/docs").headers["content-security-policy"]  # Swagger UI still works


def test_hsts_on_https(app, monkeypatch):
    monkeypatch.setattr(config, "HTTPS", True)
    assert "max-age" in TestClient(app).get("/api/health").headers["strict-transport-security"]


def test_request_size_limit(app, monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUEST_BYTES", 1000)
    hr = as_role(app, "hr")
    r = hr.post("/api/enrollment/check", content=b"x" * 2000, headers={"content-type": "image/jpeg"})
    assert r.status_code == 413 and "too large" in r.json()["detail"]

    def chunks():  # no Content-Length: counted as it arrives
        for _ in range(4):
            yield b"y" * 500

    r = hr.post("/api/enrollment/check", content=chunks(), headers={"content-type": "image/jpeg"})
    assert r.status_code == 413
    assert hr.get("/api/auth/me").status_code == 200  # small requests are fine


def test_login_rate_limit(app, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_RATE_LIMIT", 3)
    t = [datetime(2026, 10, 22, 9, 0)]
    monkeypatch.setattr(auth, "now", lambda: t[0])
    c = TestClient(app)
    for _ in range(3):  # three wrong user names: answered normally
        assert c.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "first-admin-password"})
    assert r.status_code == 429 and r.headers["retry-after"] == "61" and "Too many login attempts" in r.json()["detail"]
    t[0] += timedelta(seconds=61)
    assert c.post("/api/auth/login", json={"username": "admin", "password": "first-admin-password"}).status_code == 200
    limited = [e for e in app.state.db.audit_entries() if e["action"] == "login_rate_limited"]
    assert [e["user"] for e in limited] == ["admin"]


def test_secure_cookie_on_https(app, monkeypatch):
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", True)
    r = TestClient(app).post("/api/auth/login", json={"username": "admin", "password": "first-admin-password"})
    assert "secure" in r.headers["set-cookie"].lower()


@pytest.mark.parametrize("host, certs, allow, error, warning", [
    ("127.0.0.1", False, False, False, False),
    ("0.0.0.0", False, False, True, False),
    ("0.0.0.0", False, True, False, True),
    ("192.168.1.10", True, False, False, False),
])
def test_https_check(monkeypatch, host, certs, allow, error, warning):
    monkeypatch.setattr(config, "API_HOST", host)
    monkeypatch.setattr(config, "SSL_CERTFILE", "cert.pem" if certs else None)
    monkeypatch.setattr(config, "SSL_KEYFILE", "key.pem" if certs else None)
    monkeypatch.setattr(config, "HTTPS", certs)
    monkeypatch.setattr(config, "ALLOW_HTTP", allow)
    e, w = run.https_problem()
    assert bool(e) == error and bool(w) == warning
    if error:
        assert "GATESENSE_SSL_CERTFILE" in e and "GATESENSE_ALLOW_HTTP=1" in e


def test_preflight_refuses_a_first_start_without_an_admin_password(monkeypatch, capsys):
    monkeypatch.delenv(config.DASHBOARD_PASSWORD_ENV)  # set for every test by the first_password fixture
    monkeypatch.setattr(run, "port_in_use", lambda: False)
    assert run.preflight(open_browser=False, build=False) == 1
    assert "No users yet" in capsys.readouterr().out
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, "first-admin-password")
    monkeypatch.setattr(web, "build_status", lambda: "ok")
    assert run.preflight(open_browser=False, build=False) == 0


def test_stop_file_stops_the_server(tmp_path):
    class FakeServer:
        should_exit = False
        started = True

    server = FakeServer()
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    (config.RUN_DIR / "_api.stop").touch()
    run.watch(server, open_browser=False)
    assert server.should_exit and not (config.RUN_DIR / "_api.stop").exists()


# --- Nightly maintenance ---------------------------------------------------------------------------------


@pytest.mark.sqlite_only  # SQLite set files (SQL Server's .bak: tests/test_mssql.py)
def test_backup_now_is_never_pruned_and_only_the_newest_nightly_sets_are_kept(app, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_KEEP", 2)
    a = as_role(app, "admin")
    made = a.post("/api/system/backup", json={}).json()
    folder = maintenance.backup_dir()
    now = datetime(2026, 10, 22, 2, 30)
    stamps = [f"{(now - timedelta(days=d)):%Y%m%d-%H%M%S}" for d in (1, 2, 3, 40)]
    for stamp in stamps:
        for name in ("attendance", "gallery", "known", "cameras"):
            ext = {"attendance": "db", "gallery": "npz", "known": "zip", "cameras": "json"}[name]
            (folder / f"{name}-nightly-{stamp}.{ext}").write_text("old")
    (folder / "attendance-v0-20260101-000000.db").write_text("before an upgrade")
    (folder / "attendance-nightly-20261021-020000.db.partial").write_text("an interrupted backup")
    gone = maintenance.prune_backups()
    assert "attendance-nightly-20261021-020000.db.partial" in gone
    pruned = {g.split("-nightly-")[1].split(".")[0] for g in gone if not g.endswith(".partial")}
    assert pruned == set(stamps[2:]) and len(gone) == 1 + 2 * 4  # the two oldest sets, every file of them
    left = {p.name for p in folder.iterdir()}
    assert made["file"] in left and "attendance-v0-20260101-000000.db" in left
    assert all(f in left for f in made["files"])  # the manual set is whole
    assert {s.stamp for s in maintenance.backup_sets() if s.kind == "nightly"} == set(stamps[:2])


def test_snapshot_cleanup_keeps_the_events(app):
    s = app.state.seed
    today = date.today()
    conn = db.connect()
    paths = {}
    for days in (0, 29, 31, 60):
        day = today - timedelta(days=days)
        snap = config.SNAPSHOT_DIR / day.isoformat() / "090000_1001_entrance.jpg"
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_bytes(b"jpeg")
        paths[days] = db.add_event(conn, s["people"]["1001"], s["cameras"]["entrance"],
                                   datetime.combine(day, datetime.min.time()), 0.7,
                                   snap.relative_to(config.DATA_DIR).as_posix())
    conn.close()
    result = maintenance.clean_snapshots(app.state.db, today)
    assert sorted(result["days"]) == sorted((today - timedelta(days=d)).isoformat() for d in (31, 60))
    assert result["files"] == 2 and result["events"] == 2 and result["retention_days"] == 30
    kept = {d.name for d in config.SNAPSHOT_DIR.iterdir()}
    assert kept == {(today - timedelta(days=d)).isoformat() for d in (0, 29)}
    rows = {e["id"]: e for e in (app.state.db.event(i) for i in paths.values())}
    assert rows[paths[60]]["snapshot_path"] is None and rows[paths[0]]["snapshot_path"]  # the events are all still there


@pytest.mark.sqlite_only  # the set's database file (SQL Server's .bak: tests/test_mssql.py)
def test_nightly_run_and_when_it_is_due(app):
    result = maintenance.run_nightly(app.state.db, datetime(2026, 10, 22, 2, 5))
    assert result["ok"] and result["backup"].startswith("attendance-nightly-20261022-0205")
    state = maintenance.read_state()
    assert state["last_date"] == "2026-10-22"
    audit_rows = [e for e in app.state.db.audit_entries() if e["action"] == "backup"]
    assert audit_rows[0]["user"] == "system" and audit_rows[0]["reason"] == "nightly backup"
    at = lambda h, d=22: datetime(2026, 10, d, h)  # noqa: E731
    assert not maintenance.due(at(23), state)  # done today
    assert not maintenance.due(at(1, 23), state)  # tomorrow, before BACKUP_HOUR
    assert maintenance.due(at(2, 23), state)
    assert maintenance.due(at(0, 25), state)  # the app was off for days: catch up at once
    assert maintenance.due(at(3), {})  # never ran
    info = as_role(app, "admin").get("/api/system").json()["maintenance"]
    assert info["last_ok"] is True and info["backup_keep"] == 14 and info["snapshot_retention_days"] == 30
    assert info["backup_copy_dir"] is None


def test_snapshot_retention_is_a_setting(app):
    a = as_role(app, "admin")
    s = {x["key"]: x for x in a.get("/api/settings").json()["settings"]}
    assert s["snapshot_retention_days"]["value"] == 30
    r = a.patch("/api/settings", json={"values": {"snapshot_retention_days": 90}, "reason": "keep a quarter"})
    assert r.status_code == 200 and config.SNAPSHOT_RETENTION_DAYS == 90
    assert json.loads(config.SETTINGS_FILE.read_text())["settings"] == {"SNAPSHOT_RETENTION_DAYS": 90}
