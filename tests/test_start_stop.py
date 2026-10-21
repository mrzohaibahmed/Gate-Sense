"""start.bat / stop.bat logic: the dashboard launcher (scripts/dashboard.py) and scripts/stop.py."""

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import config, workers
from tests.test_workers import FAKE, wait_for

ROOT = Path(__file__).resolve().parent.parent


def load(script: str):
    spec = importlib.util.spec_from_file_location(f"script_{script}", ROOT / "scripts" / f"{script}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def python() -> tuple[str, dict]:
    env = dict(subprocess.os.environ)
    return workers.python_command(sys.executable, env), env


@pytest.fixture
def cameras(monkeypatch, tmp_path):
    """Config cameras that the fake worker can stand in for (a webcam and an existing file)."""
    cams = [{"name": "a", "source": 0, "direction": "IN"}, {"name": "c", "source": str(FAKE), "direction": "OUT"}]
    monkeypatch.setattr(config, "CAMERAS", cams)
    return cams


# --- Launcher ------------------------------------------------------------------------


def test_supervise_stops_workers_then_streamlit(monkeypatch):
    dash = load("dashboard")
    stopped = []
    monkeypatch.setattr(dash, "stop_workers", lambda: stopped.append("workers"))
    exe, env = python()
    proc = subprocess.Popen([exe, "-c", "import time; time.sleep(60)"], env=env)  # stands in for Streamlit
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    workers.stop_path(workers.DASHBOARD).touch()
    try:
        assert dash.supervise(proc, open_browser=False) == 0
        assert stopped == ["workers"] and proc.poll() is not None
    finally:
        proc.kill()


def test_supervise_returns_when_streamlit_exits(monkeypatch):
    dash = load("dashboard")
    exe, env = python()
    proc = subprocess.Popen([exe, "-c", "raise SystemExit(5)"], env=env)
    assert dash.supervise(proc, open_browser=False) == 5


def test_preflight_codes(monkeypatch, tmp_path):
    dash = load("dashboard")
    opened = []
    monkeypatch.setattr(dash.webbrowser, "open", opened.append)
    monkeypatch.setattr(dash, "port_in_use", lambda: False)
    assert dash.preflight() == 1  # no password anywhere

    env_file = tmp_path / ".env"
    env_file.write_text(f"{config.DASHBOARD_PASSWORD_ENV}=pw\n")
    monkeypatch.setattr(config, "ENV_PATH", env_file)
    assert dash.preflight() == 0

    monkeypatch.setattr(dash, "port_in_use", lambda: True)
    assert dash.preflight() == 1  # port taken by something else

    monkeypatch.setattr(dash.workers, "dashboard_pid", lambda: 1234)
    assert dash.preflight() == dash.ALREADY_RUNNING and opened == [dash.url()]


def test_preflight_never_prints_password(monkeypatch, tmp_path, capsys):
    dash = load("dashboard")
    monkeypatch.setattr(dash, "port_in_use", lambda: False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{config.DASHBOARD_PASSWORD_ENV}=very-secret-pw\n")
    monkeypatch.setattr(config, "ENV_PATH", env_file)
    dash.preflight()
    out = capsys.readouterr().out
    assert "found (.env)" in out and "very-secret-pw" not in out


# --- stop.py ------------------------------------------------------------------------------


def test_stop_when_nothing_runs(cameras, capsys):
    assert load("stop").main() == 0
    out = capsys.readouterr().out
    assert "No camera workers running." in out and "Dashboard not running." in out


def test_stop_stops_workers_and_dashboard(cameras, capsys):
    # Fake workers using the configured run dir, where stop.py looks.
    m = workers.WorkerManager(script=FAKE, extra_args=("--no-window", "--run-dir", str(config.RUN_DIR)))
    m.start("a")
    m.start("c")
    wait_for(lambda: m.status("a").state == "running" and m.status("c").state == "running")

    # A stand-in dashboard launcher: registered like the real one, exits when its stop file appears.
    exe, env = python()
    stop_file = workers.stop_path(workers.DASHBOARD)
    fake_dash = subprocess.Popen([exe, "-c", (
        "import pathlib, time, sys\n"
        f"p = pathlib.Path(r'{stop_file}')\n"
        "while not p.exists(): time.sleep(0.1)\n")], env=env)
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    workers.status_path(workers.DASHBOARD).write_text(f'{{"pid": {fake_dash.pid}}}')
    try:
        assert workers.dashboard_pid() == fake_dash.pid
        assert load("stop").main() == 0
        out = capsys.readouterr().out
        assert "Camera a: stopped cleanly" in out and "Camera c: stopped cleanly" in out
        assert "Dashboard stopped." in out
        assert [s.state for s in m.statuses()] == ["stopped", "stopped"]
        assert fake_dash.wait(5) == 0
    finally:
        fake_dash.kill()
        for proc in m._procs.values():
            proc.kill()


def test_stale_dashboard_info_is_cleaned(cameras, capsys):
    exe, env = python()
    dead = subprocess.Popen([exe, "-c", "pass"], env=env)
    dead.wait()
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    info = workers.status_path(workers.DASHBOARD)
    info.write_text(f'{{"pid": {dead.pid}}}')
    assert workers.dashboard_pid() is None
    assert load("stop").main() == 0 and not info.exists()


# --- The batch files themselves ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["start.bat", "stop.bat"])
def test_batch_files_use_crlf_and_the_venv(name):
    data = (ROOT / name).read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    text = data.decode()
    assert 'cd /d "%~dp0"' in text and r".venv\Scripts\python.exe" in text
