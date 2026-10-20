"""Admin password from the environment or .env, for the launcher and the dashboard."""

import importlib.util
import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app import config
from app.env import admin_password_source

ROOT = Path(__file__).resolve().parent.parent
MAIN = str(ROOT / "dashboard" / "main.py")
NAME = config.DASHBOARD_PASSWORD_ENV


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    def write(password: str) -> Path:
        path = tmp_path / ".env"
        path.write_text(f"{NAME}={password}\n", encoding="utf-8")
        monkeypatch.setattr(config, "ENV_PATH", path)
        return path

    return write


@pytest.fixture
def launcher(monkeypatch):
    """scripts/dashboard.py with subprocess.call replaced, recording the env Streamlit would get."""
    spec = importlib.util.spec_from_file_location("dashboard_launcher", ROOT / "scripts" / "dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod.subprocess, "call", lambda cmd, cwd=None: calls.append(dict(os.environ)) or 0)
    mod.calls = calls
    return mod


def login(password: str) -> AppTest:
    at = AppTest.from_file(MAIN, default_timeout=30).run()
    at.text_input(key="password").input(password)
    at.button[0].click().run()
    return at


# --- Loader --------------------------------------------------------------------


def test_password_loaded_from_env_file(env_file):
    env_file("from-file")
    assert admin_password_source() == ".env"
    assert os.environ[NAME] == "from-file"


def test_real_environment_wins_over_env_file(env_file, monkeypatch):
    env_file("from-file")
    monkeypatch.setenv(NAME, "from-environment")
    assert admin_password_source() == "environment"
    assert os.environ[NAME] == "from-environment"


def test_missing_in_both():
    assert admin_password_source() is None
    assert NAME not in os.environ


# --- Launcher ------------------------------------------------------------------


def test_launcher_uses_env_file_and_hides_password(env_file, launcher, capsys):
    env_file("secret-from-file")
    assert launcher.main() == 0
    assert launcher.calls[0][NAME] == "secret-from-file"  # passed on to Streamlit
    out = capsys.readouterr().out
    assert "found (.env)" in out and "secret-from-file" not in out


def test_launcher_prefers_real_environment(env_file, launcher, monkeypatch, capsys):
    env_file("secret-from-file")
    monkeypatch.setenv(NAME, "secret-from-environment")
    assert launcher.main() == 0
    assert launcher.calls[0][NAME] == "secret-from-environment"
    out = capsys.readouterr().out
    assert "found (environment)" in out and "secret-from" not in out


def test_launcher_refuses_without_password(launcher, capsys):
    assert launcher.main() == 1
    assert launcher.calls == []  # Streamlit never started
    assert "No admin password found" in capsys.readouterr().out


# --- Dashboard -------------------------------------------------------------------


def test_dashboard_login_with_env_file_password(env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "attendance.db")
    env_file("dash-from-file")
    at = login("dash-from-file")
    assert not at.exception and not at.error
    assert at.session_state["authenticated"]


def test_dashboard_real_environment_wins(env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "attendance.db")
    env_file("dash-from-file")
    monkeypatch.setenv(NAME, "dash-from-environment")
    assert login("dash-from-file").error[0].value == "Wrong password."
    assert login("dash-from-environment").session_state["authenticated"]


def test_dashboard_refuses_without_password():
    at = AppTest.from_file(MAIN, default_timeout=30).run()
    assert NAME in at.error[0].value and ".env" in at.error[0].value
    assert not at.text_input
