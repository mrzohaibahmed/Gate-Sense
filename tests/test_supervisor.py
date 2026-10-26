"""The supervisor (api/supervisor.py): enabled cameras start with the app and are restarted with backoff when they
stop, except when someone asked them to stop. Unit tests with a fake worker manager, then the API end to end with
fake worker processes (tests/fake_worker.py)."""

import os
import signal
import time

import pytest
from fastapi.testclient import TestClient

from api.db import Database
from api.main import create_app
from api.supervisor import Supervisor
from api.workers import CameraBusy, WorkerInfo, WorkerManager
from app import camera_settings, config
from app.workers import WorkerManager as AppWorkerManager
from tests.test_data_api import FAKE, as_role, clip, items, wait_for  # noqa: F401  (first_password: autouse there)
from tests.test_data_api import first_password  # noqa: F401


class FakeWorkers:
    def __init__(self, cameras):
        self.cameras = cameras
        self.infos = {c["name"]: WorkerInfo(camera=c["name"], state="stopped") for c in cameras}
        self.started: list[str] = []
        self.refuse: str | None = None

    def status(self, name):
        return self.infos[name]

    def start(self, name):
        if self.refuse:
            raise CameraBusy(self.refuse)
        self.started.append(name)
        self.infos[name] = WorkerInfo(camera=name, state="starting", pid=1000 + len(self.started))
        return self.infos[name]

    def run(self, name):
        self.infos[name] = WorkerInfo(camera=name, state="running", pid=self.infos[name].pid)

    def stopped(self, name, detail):
        self.infos[name] = WorkerInfo(camera=name, state="stopped", detail=detail)


@pytest.fixture
def setup():
    clock = [1000.0]
    fake = FakeWorkers([{"name": "gate", "enabled": True}, {"name": "spare", "enabled": False}])
    lines = []
    sup = Supervisor(fake, clock=lambda: clock[0], log=lines.append)
    return sup, fake, clock, lines


def advance(clock, sup, seconds):
    clock[0] += seconds
    return sup.tick()


def test_enabled_cameras_start_with_the_app(setup):
    sup, fake, clock, lines = setup
    assert sup.tick() == ["gate"]  # the disabled one is left alone
    assert "Camera gate started automatically (process 1001): enabled: started with GateSense" in lines[-1]
    fake.run("gate")
    assert advance(clock, sup, 2) == []


def test_a_crashed_worker_is_restarted_with_backoff(setup):
    sup, fake, clock, lines = setup
    sup.tick()
    fake.run("gate")
    delays = []
    for _ in range(9):
        fake.stopped("gate", "exit code 1")
        sup.tick()  # sees it stopped: schedules the restart
        waited = 0
        while not advance(clock, sup, 1):
            waited += 1
            assert waited < 400
        delays.append(waited + 1)
        fake.run("gate")
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]  # 5 s, doubling, at most 5 minutes
    assert "Camera gate stopped (exit code 1); starting it again in 5 s" in lines[1]
    assert "restarted after it stopped (exit code 1)" in lines[-1]


def test_a_stable_run_resets_the_backoff(setup):
    sup, fake, clock, _ = setup
    sup.tick()
    for _ in range(3):  # three quick crashes: the next wait would be 40 s
        fake.run("gate")
        fake.stopped("gate", "error")
        sup.tick()
        while not advance(clock, sup, 1):
            pass
    fake.run("gate")
    sup.tick()
    advance(clock, sup, config.SUPERVISOR_STABLE_SECONDS)  # ran five minutes
    fake.stopped("gate", "error")
    sup.tick()
    assert advance(clock, sup, 4) == [] and advance(clock, sup, 1) == ["gate"]  # 5 s again


def test_a_requested_stop_is_respected_until_started_by_hand(setup):
    sup, fake, clock, lines = setup
    sup.tick()
    fake.run("gate")
    fake.stopped("gate", "stop requested")  # stop.bat, another API, the command line
    sup.tick()
    assert sup.paused("gate") and "stays stopped" in lines[-1]
    assert advance(clock, sup, 3600) == []
    sup.started_by_hand("gate")  # Start on the Live cameras page
    fake.start("gate")
    fake.run("gate")
    sup.tick()
    fake.stopped("gate", "lost the camera")
    sup.tick()
    assert advance(clock, sup, 5) == ["gate"]  # watched again


def test_stop_by_hand_before_the_worker_stops(setup):
    sup, fake, clock, _ = setup
    sup.tick()
    fake.run("gate")
    sup.stopped_by_hand("gate")
    fake.stopped("gate", "killed after 10 s")  # whatever the reason, it was asked for
    assert advance(clock, sup, 600) == []


def test_a_refused_start_is_retried_and_reported_once(setup):
    sup, fake, clock, lines = setup
    fake.refuse = "webcam 0 is in use by the enrollment wizard (hr)"
    assert sup.tick() == []
    assert advance(clock, sup, 4) == [] and advance(clock, sup, 1) == []  # 5 s, refused again
    assert advance(clock, sup, 10) == []
    assert sum("can't start yet" in line for line in lines) == 1
    fake.refuse = None
    assert advance(clock, sup, 20) == ["gate"]


def test_turning_auto_on_starts_at_the_next_check(setup):
    sup, fake, clock, _ = setup
    fake.cameras[1]["enabled"] = True
    lines = []
    sup.log = lines.append
    sup.enabled_changed("spare", True)
    assert sorted(sup.tick()) == ["gate", "spare"]
    assert any("spare started automatically" in m and "started when Auto was turned on" in m for m in lines)


def test_no_starts_after_close(setup):
    sup, fake, clock, _ = setup
    sup.close()
    assert sup.tick() == [] and fake.started == []


def test_automatic_starts_are_audited():
    database = Database()
    database.connect().close()
    fake = FakeWorkers([{"name": "gate", "enabled": True}])
    Supervisor(fake, database, log=lambda m: None).tick()
    row = database.audit_entries()[0]
    assert (row["user"], row["action"], row["target"], row["reason"]) == (
        "system", "start_camera", "camera:gate", "enabled: started with GateSense")


SLOW = 60  # seconds: worker processes start slowly on a busy PC (the full suite next to a web build)


# --- End to end: the API with fake worker processes ------------------------------------------------------------


@pytest.fixture
def auto_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SUPERVISOR_CHECK_SECONDS", 0.1)
    monkeypatch.setattr(config, "SUPERVISOR_BACKOFF", (0.5,))
    video = clip(tmp_path / "c.avi")
    camera_settings.save([
        {"name": "c", "direction": "OUT", "mirror": False, "enabled": True, "type": "file", "path": str(video),
         "source": str(video)},
        {"name": "d", "direction": "IN", "mirror": False, "type": "file", "path": str(video), "source": str(video)},
    ])
    run, logs, live_dir = tmp_path / "run", tmp_path / "logs", tmp_path / "live"
    inner = AppWorkerManager(None, script=FAKE, run_dir=run, log_dir=logs, live_dir=live_dir,
                             extra_args=("--no-window", "--run-dir", str(run), "--frames", "--live-dir", str(live_dir)))
    application = create_app(workers=WorkerManager(inner))
    try:
        with TestClient(application):
            yield application, inner
    finally:
        for proc in inner._procs.values():
            proc.kill()


def test_enabled_camera_starts_restarts_and_obeys_stop(auto_app):
    app, inner = auto_app
    a = as_role(app, "admin")
    cams = {c["name"]: c for c in items(a.get("/api/cameras"))}
    assert cams["c"]["enabled"] and not cams["d"]["enabled"]
    camera = lambda name: a.get(f"/api/cameras/{cams[name]['id']}").json()  # noqa: E731
    first = wait_for(lambda: camera("c")["worker"]["state"] == "running" and camera("c")["worker"]["pid"], SLOW)
    assert camera("d")["worker"]["state"] == "stopped"  # Auto off: not started

    os.kill(first, signal.SIGTERM)  # the worker dies (a crash): restarted after the backoff
    second = wait_for(lambda: (pid := camera("c")["worker"]["pid"]) and pid != first and pid, SLOW)
    assert second != first
    audit = [e for e in app.state.db.audit_entries() if e["action"] == "start_camera"]
    assert [e["user"] for e in audit[:2]] == ["system", "system"]
    assert audit[0]["reason"].startswith("enabled: restarted after it stopped")

    r = a.post(f"/api/cameras/{cams['c']['id']}/stop")
    assert r.status_code == 200 and r.json()["result"] == "stopped cleanly"
    time.sleep(1.5)  # three backoffs: still stopped, because it was asked for
    cam = camera("c")
    assert cam["worker"]["state"] == "stopped" and cam["auto_paused"]

    assert a.post(f"/api/cameras/{cams['c']['id']}/start").status_code == 200
    wait_for(lambda: camera("c")["worker"]["state"] == "running", SLOW)
    assert not camera("c")["auto_paused"]


def test_auto_toggle_endpoint(auto_app):
    app, _ = auto_app
    a = as_role(app, "admin")
    d = next(c for c in items(a.get("/api/cameras")) if c["name"] == "d")
    r = a.put(f"/api/cameras/{d['id']}/enabled", json={"enabled": True, "reason": "second gate goes live"})
    assert r.status_code == 200 and r.json()["enabled"] is True
    wait_for(lambda: a.get(f"/api/cameras/{d['id']}").json()["worker"]["state"] == "running", SLOW)
    assert config.CAMERAS[1]["enabled"] is True
    row = next(e for e in app.state.db.audit_entries() if e["action"] == "enable_camera")
    assert (row["user"], row["target"], row["reason"]) == ("admin", "camera:d", "second gate goes live")

    r = a.put(f"/api/cameras/{d['id']}/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert a.get(f"/api/cameras/{d['id']}").json()["worker"]["state"] != "stopped"  # Off doesn't stop it
    assert as_role(app, "hr").put(f"/api/cameras/{d['id']}/enabled", json={"enabled": True}).status_code == 403
