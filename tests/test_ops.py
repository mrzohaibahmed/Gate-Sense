"""Running GateSense for months: worker logs rotated by size while running, per-frame lines only in debug, quiet
access logs for the streaming routes, ONNX threads per worker, the health report, and a webcam reader that survives
driver errors."""

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import health, maintenance
from api.main import QuietAccessLog, create_app
from api.workers import WorkerInfo
from app import config, logfile, models
from app.camera import LiveCapture
from app.workers import WorkerManager
from tests.test_live import FakeCap, frame, wait_for
from tests.test_pipeline import run, unit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
FAKE = Path(__file__).with_name("fake_worker.py")


# --- Log rotation ------------------------------------------------------------------------------------------------


def test_rotate_files_keeps_the_newest_backups(tmp_path):
    log = tmp_path / "gate.log"
    for i in range(7):
        log.write_text(f"run {i}")
        logfile.rotate_files(log, 3)
    assert not log.exists()
    assert [(tmp_path / f"gate.log.{n}").read_text() for n in (1, 2, 3)] == ["run 6", "run 5", "run 4"]
    assert not (tmp_path / "gate.log.4").exists()


def test_rotating_log_rotates_while_writing(tmp_path):
    log = logfile.RotatingLog(tmp_path / "gate.log", max_bytes=1000, backups=2, redirect_fds=False)
    for i in range(100):
        log.write(f"line {i:03d} " + "x" * 40 + "\n")
    log.close()
    assert log.rotations >= 4
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["gate.log", "gate.log.1", "gate.log.2"]  # never more than the backups
    assert all(p.stat().st_size < 1100 for p in tmp_path.iterdir())
    assert (tmp_path / "gate.log").read_text().splitlines()[-1].startswith("line 099")


def test_a_rotation_blocked_by_a_reader_is_retried_later(tmp_path, monkeypatch):
    calls = []
    real = logfile.rotate_files

    def busy_once(path, backups):
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError("the dashboard is reading the log tail")
        real(path, backups)

    monkeypatch.setattr(logfile, "rotate_files", busy_once)
    monkeypatch.setattr(logfile, "ROTATE_RETRY_BYTES", 300)
    log = logfile.RotatingLog(tmp_path / "gate.log", max_bytes=500, backups=2, redirect_fds=False)
    for _ in range(40):
        log.write("y" * 49 + "\n")  # writing never fails because of it
    log.close()
    assert len(calls) >= 2 and log.rotations == len(calls) - 1  # only the first was refused
    assert (tmp_path / "gate.log.1").exists() and (tmp_path / "gate.log").read_text().endswith("y" * 49 + "\n")


def test_a_running_worker_rotates_its_log_including_native_output(tmp_path, monkeypatch):
    """The real protocol: the manager starts a worker with --log-file; the worker's prints and what it writes to the
    stderr descriptor directly (like FFmpeg) go to the log, which is rotated while the worker keeps running."""
    monkeypatch.setenv("FAKE_WORKER_LOG_MAX_BYTES", "20000")
    run_dir, logs = tmp_path / "run", tmp_path / "logs"
    m = WorkerManager([{"name": "c", "source": str(FAKE), "direction": "OUT"}], script=FAKE,
                      extra_args=("--no-window", "--run-dir", str(run_dir), "--chatty", "500"),
                      run_dir=run_dir, log_dir=logs)
    try:
        m.start("c")
        wait_for(lambda: (logs / "c.log.2").exists(), timeout=60)
        assert m.status("c").state in ("running", "starting")  # still running after rotating
    finally:
        assert m.stop("c") == "stopped cleanly"
    current = (logs / "c.log").read_text(errors="replace")
    assert "Worker stopped: stop requested" in current  # written after the rotations, into the new file
    rotated = (logs / "c.log.1").read_text(errors="replace")
    assert "native" in rotated and "x" * 100 in rotated
    assert all(p.stat().st_size < 40000 for p in logs.iterdir())
    assert sum(1 for p in logs.iterdir() if p.name.startswith("c.log.")) <= config.WORKER_LOG_BACKUPS


def test_the_manager_passes_the_log_file_to_the_worker(tmp_path):
    m = WorkerManager([{"name": "c", "source": str(FAKE), "direction": "OUT"}], script=FAKE,
                      extra_args=("--no-window", "--run-dir", str(tmp_path / "run")), run_dir=tmp_path / "run",
                      log_dir=tmp_path / "logs")
    try:
        proc = m.start("c") and m._procs["c"]
        assert proc.args[-2:] == ["--log-file", str(tmp_path / "logs" / "c.log")]
    finally:
        m.stop("c")


# --- Per-frame lines only in debug -----------------------------------------------------------------------------


def test_quality_skips_are_counted_not_logged_unless_debug(capsys, monkeypatch):
    p, decisions = run(unit(0), kind="blurry", frames=4)
    assert [d[0] for d in decisions] == ["skip"] * 4  # still shown in the live decisions
    assert "skip" not in capsys.readouterr().out  # but not one log line per frame
    assert "skip: blurry 4" in p.stats_line()  # the once-a-minute summary counts them

    monkeypatch.setattr(config, "WORKER_DEBUG_LOG", True)
    run(unit(0), kind="blurry", frames=2)
    assert capsys.readouterr().out.count(" skip ") == 2


# --- Quiet access log ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path, status, shown", [
    ("/api/cameras/3/snapshot.jpg", 200, False),
    ("/api/cameras/3/stream", 200, False),
    ("/api/cameras/3/stream?max_frames=5", 200, False),
    ("/api/enrollment/sessions/abc123/stream", 200, False),
    ("/api/snapshots/42", 200, False),
    ("/api/cameras/3/snapshot.jpg", 404, True),  # errors still show
    ("/api/cameras/3/stream", 409, True),
    ("/api/cameras", 200, True),
    ("/api/attendance", 200, True),
    ("/api/cameras/3/start", 200, True),
])
def test_quiet_access_log(path, status, shown):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "GET", path, "1.1", status), None)
    assert QuietAccessLog().filter(record) is shown


def test_the_app_installs_the_filter_once():
    create_app()
    create_app()
    assert sum(isinstance(f, QuietAccessLog) for f in logging.getLogger("uvicorn.access").filters) == 1


# --- ONNX threads --------------------------------------------------------------------------------------------


def test_onnx_threads(monkeypatch):
    assert models.onnx_threads(cameras=2, cores=4) == 2
    assert models.onnx_threads(cameras=3, cores=4) == 1
    assert models.onnx_threads(cameras=4, cores=2) == 1  # never 0
    assert models.onnx_threads(cameras=0, cores=8) == 8
    monkeypatch.setattr(config, "CAMERAS", [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}])
    assert models.onnx_threads(cores=8) == 2  # the cameras in the list
    monkeypatch.setattr(config, "ONNX_THREADS", 3)
    assert models.onnx_threads(cameras=4, cores=8) == 3
    assert models.session_options().intra_op_num_threads == 3


def test_onnx_threads_is_an_editable_setting():
    assert config.check_setting("ONNX_THREADS", 0) == 0
    with pytest.raises(ValueError):
        config.check_setting("ONNX_THREADS", -1)


# --- Health ---------------------------------------------------------------------------------------------------


class Supervisor:
    def __init__(self, paused=()):
        self._paused = set(paused)

    def paused(self, name):
        return name in self._paused


class Workers:
    def __init__(self, cameras, infos):
        self.cameras, self._infos = cameras, infos

    def statuses(self):
        return self._infos


NOW = datetime(2026, 10, 24, 12, 0)


def test_camera_health_problems():
    cams = [{"name": n, "enabled": n != "off"} for n in ("ok", "down", "paused", "off", "hung", "stale", "dark")]
    infos = [
        WorkerInfo(camera="ok", state="running", started=NOW - timedelta(hours=1), last_frame_at=NOW),
        WorkerInfo(camera="down", state="stopped", last_error="lost the camera"),
        WorkerInfo(camera="paused", state="stopped"),
        WorkerInfo(camera="off", state="stopped"),
        WorkerInfo(camera="hung", state="not responding"),
        WorkerInfo(camera="stale", state="running", started=NOW - timedelta(hours=1),
                   last_frame_at=NOW - timedelta(seconds=95), warning="camera reconnecting"),
        WorkerInfo(camera="dark", state="running", started=NOW - timedelta(minutes=5)),
    ]
    out = {c.name: c for c in health.camera_health(Workers(cams, infos), Supervisor({"paused"}), NOW)}
    assert out["ok"].problem is None and out["ok"].last_frame_age_seconds == 0
    assert out["down"].problem == "down is stopped (Auto is on: it is being restarted); last error: lost the camera"
    assert out["paused"].problem is None and out["paused"].auto_paused  # stopped by hand: on purpose
    assert out["off"].problem is None  # Auto off: on purpose
    assert out["hung"].problem == "hung is not responding"
    assert out["stale"].problem == "stale: no picture for 95 s (camera reconnecting)"
    assert out["dark"].problem == "dark: no picture since it started 300 s ago"


def test_disk_health(tmp_path, monkeypatch):
    assert health.disk_health(tmp_path).problem is None  # conftest: no minimum
    monkeypatch.setattr(config, "HEALTH_MIN_FREE_BYTES", 10**18)
    d = health.disk_health(tmp_path)
    assert d.free_bytes > 0 and d.total_bytes >= d.free_bytes and "free on the data disk" in d.problem


def test_backup_health():
    long_up = 10 * config.MAINTENANCE_CHECK_SECONDS
    ok = {"last_date": "2026-10-24", "last": {"time": "2026-10-24T02:05:00", "ok": True}}
    assert health.backup_health(long_up, NOW, ok).problem is None
    failed = {"last_date": "2026-10-24", "last": {"time": "2026-10-24T02:05:00", "ok": False,
                                                  "error": "copy to E:\\backups failed"}}
    assert health.backup_health(long_up, NOW, failed).problem == (
        "the last nightly backup (2026-10-24 02:05) failed: copy to E:\\backups failed")
    yesterday = {"last_date": "2026-10-23", "last": {"time": "2026-10-23T02:05:00", "ok": True}}
    assert "overdue (last one: 2026-10-23 02:05)" in health.backup_health(long_up, NOW, yesterday).problem
    assert health.backup_health(60, NOW, yesterday).problem is None  # just started: the catch-up is still to come
    early = NOW.replace(hour=2, minute=5)
    assert health.backup_health(long_up, early, yesterday).problem is None  # due 5 minutes ago: not overdue yet
    assert "never" in health.backup_health(long_up, NOW, {}).problem


def test_health_endpoint_lists_the_problems(monkeypatch):
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, "first-admin-password")  # the first admin, at start
    monkeypatch.setattr(config, "HEALTH_MIN_FREE_BYTES", 10**18)
    with TestClient(create_app()) as client:
        body = client.get("/api/health").json()
    assert body["status"] == "degraded" and [c["name"] for c in body["cameras"]] == ["entrance", "exit"]
    assert any("free on the data disk" in p for p in body["problems"])
    assert body["disk"]["free_bytes"] > 0 and body["backup"]["last_run"] is None


# --- Webcam reader -----------------------------------------------------------------------------------------------


class RaisingCap(FakeCap):
    def __init__(self):
        super().__init__([frame(5)])
        self.calls = 0

    def read(self):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("driver error in read")
        return super().read()

    def release(self):
        super().release()
        raise RuntimeError("driver error in release")


def test_capture_survives_driver_errors_in_open_read_and_release():
    opens = []

    def opener(source):
        opens.append(source)
        if len(opens) == 1:
            raise OSError("some other driver error")  # not a RuntimeError: still retried
        if len(opens) == 2:
            return None  # nothing at all
        if len(opens) == 3:
            return RaisingCap()  # one frame, then read() and release() raise
        return FakeCap([frame(77)] * 50)

    cap = LiveCapture(0, opener=opener, max_failures=3, backoff=(0.02,))
    wait_for(lambda: (got := cap.latest()) and int(got[1].mean()) == 77)
    assert len(opens) == 4 and cap.error is None and cap.reconnects == 3
    assert cap._thread.is_alive()
    cap.stop()
