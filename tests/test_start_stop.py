"""stop.bat logic (scripts/stop.py) and the batch files themselves (start.bat's checks are api.run --preflight,
tested in test_production.py)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from api.workers import api_pid
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


# --- stop.py ------------------------------------------------------------------------------


def test_stop_when_nothing_runs(cameras, capsys):
    assert load("stop").main() == 0
    out = capsys.readouterr().out
    assert "No camera workers running." in out and "GateSense not running." in out


def test_stop_stops_workers_and_the_app(cameras, capsys):
    # Fake workers using the configured run dir, where stop.py looks.
    m = workers.WorkerManager(script=FAKE, extra_args=("--no-window", "--run-dir", str(config.RUN_DIR)))
    m.start("a")
    m.start("c")
    wait_for(lambda: m.status("a").state == "running" and m.status("c").state == "running")

    # A stand-in app (python -m api.run): registered like the real one, exits when its stop file appears.
    exe, env = python()
    stop_file = workers.stop_path("_api")
    fake_app = subprocess.Popen([exe, "-c", (
        "import pathlib, time, sys\n"
        f"p = pathlib.Path(r'{stop_file}')\n"
        "while not p.exists(): time.sleep(0.1)\n")], env=env)
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    workers.status_path("_api").write_text(f'{{"pid": {fake_app.pid}}}')
    try:
        assert api_pid() == fake_app.pid
        assert load("stop").main() == 0
        out = capsys.readouterr().out
        assert "Camera a: stopped cleanly" in out and "Camera c: stopped cleanly" in out
        assert "GateSense stopped." in out
        assert [s.state for s in m.statuses()] == ["stopped", "stopped"]
        assert fake_app.wait(5) == 0
    finally:
        fake_app.kill()
        for proc in m._procs.values():
            proc.kill()


def test_stale_app_info_is_cleaned(cameras, capsys):
    exe, env = python()
    dead = subprocess.Popen([exe, "-c", "pass"], env=env)
    dead.wait()
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    info = workers.status_path("_api")
    info.write_text(f'{{"pid": {dead.pid}}}')
    assert api_pid() is None
    assert load("stop").main() == 0 and not info.exists()


# --- The batch files themselves ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["start.bat", "stop.bat"])
def test_batch_files_use_crlf_and_the_venv(name):
    data = (ROOT / name).read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    text = data.decode()
    assert 'cd /d "%~dp0"' in text and r".venv\Scripts\python.exe" in text
