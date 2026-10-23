"""The FastAPI app (health, CORS, lifespan) and the API's WorkerManager.

Workers are tests/fake_worker.py processes reading a fake frame source: no camera, no models.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import __version__
from api.db import Database
from api.main import create_app
from api.workers import CameraBusy, WorkerManager, api_pid
from app import config, db, workers
from app.workers import WorkerManager as AppWorkerManager

FAKE = Path(__file__).with_name("fake_worker.py")
CAMERAS = [
    {"name": "a", "source": 0, "direction": "IN"},
    {"name": "b", "source": 0, "direction": "OUT"},  # same webcam as a
    {"name": "c", "source": str(FAKE), "direction": "OUT"},  # any existing file stands in for a clip
]


@pytest.fixture(autouse=True)
def admin_password(monkeypatch):
    """The API creates its first admin from this on start (tests/test_auth.py covers that)."""
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, "first-admin-password")


def wait_for(predicate, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "run", tmp_path / "logs", tmp_path / "live"


@pytest.fixture
def procs():
    """Processes a test launches itself; killed at the end."""
    started = []
    yield started
    for proc in started:
        proc.kill()


@pytest.fixture
def make_manager(dirs):
    run, logs, live_dir = dirs
    made = []

    def make(*extra) -> WorkerManager:
        inner = AppWorkerManager(
            CAMERAS, script=FAKE, run_dir=run, log_dir=logs, live_dir=live_dir,
            extra_args=("--no-window", "--run-dir", str(run), "--frames", "--live-dir", str(live_dir), *extra))
        made.append(inner)
        return WorkerManager(inner)

    yield make
    for inner in made:  # never leave fake workers behind
        for proc in inner._procs.values():
            proc.kill()


def python() -> tuple[str, dict]:
    env = dict(os.environ)
    return workers.python_command(sys.executable, env), env


def launch(procs, run: Path, camera: str, *extra) -> subprocess.Popen:
    """A fake worker started by someone else (the dashboard or the command line), not by the manager."""
    exe, env = python()
    proc = subprocess.Popen([exe, str(FAKE), "--camera", camera, "--run-dir", str(run), *extra],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    procs.append(proc)
    return proc


def sleeper(procs) -> subprocess.Popen:
    exe, env = python()
    proc = subprocess.Popen([exe, "-c", "import time; time.sleep(120)"], env=env)
    procs.append(proc)
    return proc


# --- Health ----------------------------------------------------------------------------


def test_health_ok():
    db.connect(config.DB_PATH).close()
    with TestClient(create_app()) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"] == __version__ == "0.1.0"
    assert body["database"]["status"] == "ok" and body["database"]["backend"] == "sqlite"
    assert 0 <= body["uptime_seconds"] < 60
    assert abs((datetime.now() - datetime.fromisoformat(body["started"])).total_seconds()) < 60


def test_status_of_a_missing_database_does_not_create_it():
    status = Database().status()
    assert status["status"] == "missing" and status["backend"] == "sqlite"
    assert not config.DB_PATH.exists()


def test_the_api_creates_the_database_on_start():
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["database"]["status"] == "ok"
    assert config.DB_PATH.is_file()


def test_health_unreadable_database_is_503():
    with TestClient(create_app()) as client:
        config.DB_PATH.write_bytes(b"this is not a database" * 100)  # broken while running
        for suffix in ("-wal", "-shm"):
            config.DB_PATH.with_name(config.DB_PATH.name + suffix).unlink(missing_ok=True)
        r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded" and r.json()["database"]["status"] == "error"


def test_postgresql_url_is_recognised_but_not_supported_yet():
    database = Database("postgresql://gatesense@localhost/gatesense")
    assert database.backend == "postgresql"
    status = database.status()
    assert status["status"] == "error" and "PostgreSQL is not supported yet" in status["detail"]
    with pytest.raises(RuntimeError, match="not supported"):
        database.connect()
    with pytest.raises(RuntimeError, match="PostgreSQL is not supported yet"):
        with TestClient(create_app(database=database)):
            pass  # the API refuses to start rather than use another database


def test_database_url_from_config(tmp_path, monkeypatch):
    path = tmp_path / "other.db"
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{path.as_posix()}")
    database = Database()
    assert database.path == path and database.status()["status"] == "missing"
    database.connect().close()  # creates the schema, like the dashboard and the workers
    assert database.status() == {"status": "ok", "backend": "sqlite", "detail": "8 tables"}
    assert not config.DB_PATH.exists()  # the default database was not touched


# --- CORS ------------------------------------------------------------------------------


def test_cors_only_for_the_vite_dev_server():
    with TestClient(create_app()) as client:
        ok = client.options("/api/health", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
        assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:5173"
        bad = client.options("/api/health", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
        assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers
        plain = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
        assert "access-control-allow-origin" not in plain.headers


# --- Lifespan ----------------------------------------------------------------------------


def test_lifespan_marks_the_api_and_stops_its_workers_on_shutdown(make_manager, dirs, procs):
    run = dirs[0]
    manager = make_manager()
    other = launch(procs, run, "b", "--parent-pid", str(sleeper(procs).pid))  # not ours: left running
    wait_for(lambda: workers.live_status("b", run))
    with TestClient(create_app(workers=manager)) as client:
        assert api_pid(run) == os.getpid()
        assert client.app.state.workers is manager
        manager.start("c")
        wait_for(lambda: manager.status("c").state == "running")
    assert manager.status("c").state == "stopped" and manager.status("c").detail == "stop requested"
    assert manager.status("b").state == "running" and other.poll() is None
    assert not workers.status_path("_api", run).exists() and api_pid(run) is None


def test_second_api_refused(make_manager, dirs, procs):
    run = dirs[0]
    run.mkdir(parents=True)
    workers.status_path("_api", run).write_text(json.dumps({"pid": sleeper(procs).pid}))
    with pytest.raises(RuntimeError, match="already running"):
        with TestClient(create_app(workers=make_manager())):
            pass


# --- WorkerManager ---------------------------------------------------------------------


def test_start_reports_fps_last_frame_and_holder(make_manager):
    m = make_manager()
    assert m.status("a").state == "stopped" and m.status("a").held_by is None
    first = m.start("a")
    assert first.state == "starting" and first.held_by == "api"
    wait_for(lambda: m.status("a").state == "running" and (m.status("a").fps or 0) > 1)
    info = m.status("a")
    assert info.held_by == "api" and info.pid and info.started and info.last_error is None
    assert info.last_frame_at and (datetime.now() - info.last_frame_at).total_seconds() < 5
    assert m.stop("a") == "stopped cleanly"
    stopped = m.status("a")
    assert stopped.state == "stopped" and stopped.detail == "stop requested" and stopped.last_error is None
    assert [s.camera for s in m.statuses()] == ["a", "b", "c"]


def test_last_error_from_the_frame_source(make_manager):
    m = make_manager("--error", "could not connect (timeout)")
    m.start("c")
    info = wait_for(lambda: (s := m.status("c")).state == "running" and s)
    assert info.last_error == "could not connect (timeout)"


def test_crashed_worker_shows_exit_code_as_last_error(make_manager):
    m = make_manager("--crash")
    m.start("a")
    info = wait_for(lambda: (s := m.status("a")).state == "stopped" and s)
    assert info.last_error == "exit code 3"


def test_camera_held_by_another_process_is_refused(make_manager, dirs, procs):
    parent = sleeper(procs)  # some other GateSense process that started the worker
    launch(procs, dirs[0], "a", "--parent-pid", str(parent.pid), "--source", "0")
    m = make_manager()
    wait_for(lambda: m.status("a").state == "running")
    assert m.status("a").held_by == "other"
    with pytest.raises(CameraBusy, match=rf"a is already running \(started by another GateSense process "
                                         rf"\({parent.pid}\), process \d+\); it can't be opened twice"):
        m.start("a")
    with pytest.raises(CameraBusy, match="webcam 0 is already in use by the a worker \\(started by another"):
        m.start("b")
    m.start("c")  # a video file doesn't conflict
    assert m.status("c").held_by == "api"


def test_command_line_worker_is_refused_too(make_manager, dirs, procs):
    launch(procs, dirs[0], "a")
    m = make_manager()
    wait_for(lambda: m.status("a").state == "running")
    assert m.status("a").held_by == "command line"
    with pytest.raises(CameraBusy, match="started from the command line"):
        m.start("a")


def test_enrollment_wizard_claim_blocks_webcam_workers(make_manager, monkeypatch):
    m = make_manager()
    claim = m.claim_webcam("hr")
    assert m.webcam_claimed_by() == ["hr"]
    with pytest.raises(CameraBusy, match=r"webcam 0 is in use by the enrollment wizard \(hr\); finish or close"):
        m.start("a")
    m.start("c")  # not a webcam
    assert m.claim_webcam("hr", claim) == claim  # a renewal keeps the id
    assert m.release_webcam(claim) and not m.release_webcam(claim)
    assert m.webcam_claimed_by() == []
    assert m.start("a").state == "starting"


def test_wizard_claim_refused_while_a_webcam_worker_runs(make_manager):
    m = make_manager()
    m.start("a")
    with pytest.raises(CameraBusy, match="the webcam is in use by the a camera worker; stop it"):
        m.claim_webcam("hr")  # refused while it is still starting, too
    wait_for(lambda: m.status("a").state == "running")
    m.stop("a")
    assert m.claim_webcam("hr")


def test_a_claim_that_is_not_renewed_lapses(make_manager, monkeypatch):
    m = make_manager()
    clock = [1000.0]
    monkeypatch.setattr("api.workers.time.monotonic", lambda: clock[0])
    old = m.claim_webcam("hr")
    clock[0] += config.ENROLL_CAMERA_CLAIM_SECONDS - 1
    assert m.webcam_claimed_by() == ["hr"]
    clock[0] += 2
    assert m.webcam_claimed_by() == []  # the tab was closed without releasing
    new = m.claim_webcam("hr", old)  # renewing a lapsed claim makes a new one
    assert new != old and m.webcam_claimed_by() == ["hr"]


def test_unknown_camera(make_manager):
    with pytest.raises(workers.WorkerError, match="unknown camera 'zzz'"):
        make_manager().start("zzz")


# --- One process per camera, even for simultaneous starts --------------------------------


def test_camera_lock_lets_only_one_process_open_a_camera(dirs, procs):
    run = dirs[0]
    first = launch(procs, run, "a", "--source", "0")
    wait_for(lambda: workers.live_status("a", run))
    second = launch(procs, run, "a", "--source", "0")  # bypasses every check, like a start at the same moment
    assert second.wait(timeout=15) == 1
    assert "already open in another worker process" in second.stdout.read()
    other_camera_same_webcam = launch(procs, run, "b", "--source", "0")
    assert other_camera_same_webcam.wait(timeout=15) == 1
    first.kill()  # the OS releases the lock even when the process is killed
    first.wait()
    third = launch(procs, run, "a", "--source", "0")
    wait_for(lambda: (s := workers.live_status("a", run)) and s["pid"] == third.pid)


def test_acquire_camera_in_process(tmp_path):
    locks = workers.acquire_camera("x", 0, tmp_path)
    assert [lock.path.name for lock in locks] == ["camera-x.lock", "webcam-0.lock"]
    assert workers.acquire_camera("y", "0", tmp_path) is None  # same webcam, other camera name
    assert workers.acquire_camera("x", "rtsp://cam/1", tmp_path) is None  # same camera name
    clip = workers.acquire_camera("z", "clip.mp4", tmp_path)
    assert [lock.path.name for lock in clip] == ["camera-z.lock"]
    for lock in locks + clip:
        lock.release()
    again = workers.acquire_camera("y", 0, tmp_path)
    assert again is not None
    for lock in again:
        lock.release()
