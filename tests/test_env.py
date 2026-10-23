"""Admin password from the environment or .env (used for the first admin account)."""

import os
from pathlib import Path

import pytest

from app import config
from app.env import admin_password_source

NAME = config.DASHBOARD_PASSWORD_ENV


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    def write(password: str) -> Path:
        path = tmp_path / ".env"
        path.write_text(f"{NAME}={password}\n", encoding="utf-8")
        monkeypatch.setattr(config, "ENV_PATH", path)
        return path

    return write


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
