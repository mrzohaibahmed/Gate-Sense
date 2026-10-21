"""${VAR} placeholders in camera sources (app/sources.py) and where they are handled."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

from app import config, sources, workers
from app.camera import mask_source
from app.sources import MissingVariables, expand

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    def write(text: str) -> Path:
        path = tmp_path / ".env"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(config, "ENV_PATH", path)
        return path

    return write


@pytest.fixture(autouse=True)
def no_cam_vars(monkeypatch):
    for name in ("GATESENSE_CAM_PASS", "GATESENSE_CAM_USER", "GATESENSE_CAM_HOST", "GATESENSE_CLIP_DIR"):
        monkeypatch.delenv(name, raising=False)


# --- Expansion --------------------------------------------------------------------------------


def test_expands_from_environment(monkeypatch):
    monkeypatch.setenv("GATESENSE_CAM_PASS", "Secret123")
    assert expand(TEMPLATE) == "rtsp://gatesense:Secret123@192.168.1.101:554/Streaming/Channels/101"


def test_expands_from_env_file(env_file):
    env_file("GATESENSE_CAM_PASS=FromFile9\n")
    assert expand(TEMPLATE) == "rtsp://gatesense:FromFile9@192.168.1.101:554/Streaming/Channels/101"


def test_environment_wins_over_env_file(env_file, monkeypatch):
    env_file("GATESENSE_CAM_PASS=FromFile9\n")
    monkeypatch.setenv("GATESENSE_CAM_PASS", "FromEnv7")
    assert "FromEnv7@" in expand(TEMPLATE)


def test_env_file_is_read_fresh_and_not_loaded_into_environ(env_file):
    path = env_file("GATESENSE_CAM_PASS=first\n")
    assert "first@" in expand(TEMPLATE)
    path.write_text("GATESENSE_CAM_PASS=second\n")  # edited while the dashboard runs
    assert "second@" in expand(TEMPLATE)
    assert "GATESENSE_CAM_PASS" not in os.environ


@pytest.mark.parametrize("password, encoded", [
    ("p@ss#word", "p%40ss%23word"),
    ("a/b:c?d", "a%2Fb%3Ac%3Fd"),
    ("100%sure", "100%25sure"),
    ("with space", "with%20space"),
    ("plain", "plain"),
])
def test_password_is_url_encoded(monkeypatch, password, encoded):
    monkeypatch.setenv("GATESENSE_CAM_PASS", password)
    url = expand(TEMPLATE)
    assert url == f"rtsp://gatesense:{encoded}@192.168.1.101:554/Streaming/Channels/101"
    assert unquote(url.split("://")[1].split("@")[0].split(":", 1)[1]) == password  # decodes back exactly


def test_special_characters_from_quoted_env_file_value(env_file):
    env_file("GATESENSE_CAM_PASS='p@ss #1'\n")  # quoted, so the " #1" isn't a comment
    assert expand(TEMPLATE).startswith("rtsp://gatesense:p%40ss%20%231@")


def test_user_is_encoded_but_host_and_path_are_not(monkeypatch):
    monkeypatch.setenv("GATESENSE_CAM_USER", "gate@sense")
    monkeypatch.setenv("GATESENSE_CAM_PASS", "pw")
    monkeypatch.setenv("GATESENSE_CAM_HOST", "192.168.1.101:554")
    url = expand("rtsp://${GATESENSE_CAM_USER}:${GATESENSE_CAM_PASS}@${GATESENSE_CAM_HOST}/Streaming/Channels/101")
    assert url == "rtsp://gate%40sense:pw@192.168.1.101:554/Streaming/Channels/101"


def test_file_paths_are_expanded_raw(monkeypatch):
    monkeypatch.setenv("GATESENSE_CLIP_DIR", r"D:\clips & tests")
    assert expand(r"${GATESENSE_CLIP_DIR}\exit.mp4") == r"D:\clips & tests\exit.mp4"


@pytest.mark.parametrize("source", [0, 1, "rtsp://cam/stream", "clip.mp4", "rtsp://u:p@host/x"])
def test_sources_without_placeholders_are_unchanged(source):
    assert expand(source) == source


# --- Missing variables ------------------------------------------------------------------------------


def test_missing_variable_is_a_clear_error():
    with pytest.raises(MissingVariables) as e:
        expand(TEMPLATE)
    assert e.value.names == ["GATESENSE_CAM_PASS"]
    assert "missing environment variable GATESENSE_CAM_PASS" in str(e.value)
    assert "GATESENSE_CAM_PASS=..." in str(e.value) and ".env" in str(e.value)


def test_empty_value_counts_as_missing(env_file, monkeypatch):
    env_file("GATESENSE_CAM_PASS=\n")
    monkeypatch.setenv("GATESENSE_CAM_PASS", "")
    assert sources.missing_variables(TEMPLATE) == ["GATESENSE_CAM_PASS"]


def test_several_missing_variables_listed():
    names = sources.missing_variables("rtsp://${GATESENSE_CAM_USER}:${GATESENSE_CAM_PASS}@host/x")
    assert names == ["GATESENSE_CAM_PASS", "GATESENSE_CAM_USER"]
    assert "variables GATESENSE_CAM_PASS, GATESENSE_CAM_USER" in str(MissingVariables(names))


# --- Never shown in clear ----------------------------------------------------------------------------


def test_expanded_url_is_masked(monkeypatch):
    monkeypatch.setenv("GATESENSE_CAM_PASS", "p@ss#word")
    assert mask_source(expand(TEMPLATE)) == "rtsp://gatesense:***@192.168.1.101:554/Streaming/Channels/101"
    assert mask_source(TEMPLATE) == "rtsp://gatesense:***@192.168.1.101:554/Streaming/Channels/101"


# --- Where it is used ---------------------------------------------------------------------------------


def test_manager_refuses_start_with_missing_variable(tmp_path):
    cams = [{"name": "gate", "source": TEMPLATE, "direction": "IN"}]
    m = workers.WorkerManager(cams, script=ROOT / "tests" / "fake_worker.py", run_dir=tmp_path / "run",
                              log_dir=tmp_path / "logs")
    with pytest.raises(workers.WorkerError, match="missing environment variable GATESENSE_CAM_PASS"):
        m.start("gate")
    assert not m.log_path("gate").exists()  # nothing launched


def test_worker_exits_cleanly_with_reason(tmp_path):
    """The real recognize.py: no crash, a clear reason in its status file (shown on the Cameras page)."""
    env = dict(os.environ, GATESENSE_DATA_DIR=str(tmp_path / "data"), GATESENSE_ENV_FILE=str(tmp_path / "no.env"))
    env.pop("GATESENSE_CAM_PASS", None)
    exe = workers.python_command(sys.executable, env)
    proc = subprocess.run([exe, str(ROOT / "scripts" / "recognize.py"), "--camera", "exit", "--no-window",
                           "--source", TEMPLATE], env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1 and "Traceback" not in proc.stdout + proc.stderr
    assert "ERROR: missing environment variable GATESENSE_CAM_PASS" in proc.stdout
    status = json.loads((tmp_path / "data" / "run" / "exit.json").read_text())
    assert status["state"] == "stopped" and status["reason"] == "missing environment variable GATESENSE_CAM_PASS"


def test_probe_reports_missing_variable(monkeypatch, capsys):
    from app import probe_camera

    monkeypatch.setattr(config, "CAMERAS", [{"name": "gate", "source": TEMPLATE, "direction": "IN"}])
    assert probe_camera.main(["gate", "--seconds", "1"]) == 1
    assert "missing environment variable GATESENSE_CAM_PASS" in capsys.readouterr().out
