"""Worker manager tests with a fake worker process (tests/fake_worker.py)."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import workers
from app.workers import WorkerError, WorkerManager

FAKE = Path(__file__).with_name("fake_worker.py")
CAMERAS = [
    {"name": "a", "source": 0, "direction": "IN"},
    {"name": "b", "source": 0, "direction": "OUT"},  # same webcam as a
    {"name": "c", "source": "clip.mp4", "direction": "OUT"},
]


def wait_for(predicate, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "run", tmp_path / "logs"


@pytest.fixture
def make_manager(dirs):
    run, logs = dirs
    made = []

    def make(*extra) -> WorkerManager:
        m = WorkerManager(CAMERAS, script=FAKE, extra_args=("--no-window", "--run-dir", str(run), *extra),
                          run_dir=run, log_dir=logs)
        made.append(m)
        return m

    yield make
    for m in made:  # never leave fake workers behind
        for proc in m._procs.values():
            proc.kill()


def running(m: WorkerManager, name: str):
    return m.status(name).state == "running"


def test_start_status_stop(make_manager):
    m = make_manager()
    assert m.status("a").state == "stopped"
    assert m.start("a").state == "starting"
    wait_for(lambda: running(m, "a"))
    st = m.status("a")
    assert st.managed and st.pid and st.started
    wait_for(lambda: (m.status("a").fps or 0) > 1)
    assert any("fake worker a started" in line for line in m.log_tail("a"))

    assert m.stop("a") == "stopped cleanly"
    st = m.status("a")
    assert st.state == "stopped" and st.detail == "stop requested"
    assert m.log_tail("a")[-1] == "Worker stopped: stop requested"
    assert m.stop("a") == "not running"


def test_start_twice_refused(make_manager):
    m = make_manager()
    m.start("a")
    with pytest.raises(WorkerError, match="already running"):
        m.start("a")


def test_same_webcam_refused(make_manager):
    m = make_manager()
    m.start("a")
    with pytest.raises(WorkerError, match="webcam 0 is already in use by the a worker"):
        m.start("b")  # a is still loading (no status file yet)
    wait_for(lambda: running(m, "a"))
    with pytest.raises(WorkerError, match="webcam 0"):
        m.start("b")  # a is running
    m.start("c")  # a video file doesn't conflict
    wait_for(lambda: running(m, "c"))
    assert m.webcam_users() == ["a"]
    assert workers.webcam_conflict("enroll", 0, CAMERAS, m.run_dir) == "a"
    assert workers.webcam_conflict("enroll", "clip.mp4", CAMERAS, m.run_dir) is None
    m.stop("a")
    m.start("b")  # free again
    wait_for(lambda: running(m, "b"))


def test_hung_worker_is_killed(make_manager):
    m = make_manager("--ignore-stop")
    m.start("a")
    wait_for(lambda: running(m, "a"))
    assert m.stop("a", timeout=1).startswith("killed")
    assert m.status("a").state == "stopped"
    assert "killed after 1 s" in m.log_tail("a")[-1]


def test_crashed_worker_shows_exit_code(make_manager):
    m = make_manager("--crash")
    m.start("a")
    st = wait_for(lambda: (s := m.status("a")).state == "stopped" and s)
    assert st.detail == "exit code 3"


def test_detects_worker_after_dashboard_restart(make_manager):
    first = make_manager()
    first.start("a")
    wait_for(lambda: running(first, "a"))
    second = make_manager()  # a new dashboard process sees the same status files
    st = second.status("a")
    assert st.state == "running" and not st.managed
    assert workers.running_elsewhere("a", second.run_dir) == st.pid
    assert second.stop("a") == "stopped cleanly"  # via the stop file
    assert first.status("a").state == "stopped"


def test_stop_all(make_manager):
    m = make_manager()
    m.start("a")
    m.start("c")
    wait_for(lambda: running(m, "a") and running(m, "c"))
    m.stop_all()
    assert [s.state for s in m.statuses()] == ["stopped"] * 3


def test_popen_pid_is_the_worker(make_manager):
    """Not the venv launcher: otherwise killing it would orphan the real worker."""
    m = make_manager()
    proc_pid = m.start("a").pid
    wait_for(lambda: running(m, "a"))
    assert m.status("a").pid == proc_pid


def python() -> tuple[str, dict]:
    env = dict(subprocess.os.environ)
    return workers.python_command(sys.executable, env), env


def test_worker_exits_when_dashboard_dies(dirs):
    run, _ = dirs
    exe, env = python()
    parent = subprocess.Popen([exe, "-c", "import time; time.sleep(60)"], env=env)
    worker = subprocess.Popen([exe, str(FAKE), "--camera", "a", "--run-dir", str(run),
                               "--parent-pid", str(parent.pid)], stdout=subprocess.DEVNULL, env=env)
    try:
        wait_for(lambda: workers.live_status("a", run))
        parent.kill()  # the "dashboard" dies without cleaning up
        assert worker.wait(timeout=15) == 0
        assert json.loads((run / "a.json").read_text())["reason"] == "dashboard exited"
    finally:
        parent.kill()
        worker.kill()


def test_pid_alive():
    assert workers.pid_alive(subprocess.os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not workers.pid_alive(proc.pid)
    assert not workers.pid_alive(None) and not workers.pid_alive(0)
