import pytest

from app import config


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never touch the real .env, known/ or data/, and undo any variable a test loads from a .env file."""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "no-such.env")
    # setenv-then-delenv records the original state, so teardown also removes a value load_dotenv set.
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, "placeholder")
    monkeypatch.delenv(config.DASHBOARD_PASSWORD_ENV)

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "KNOWN_DIR", tmp_path / "known")
    monkeypatch.setattr(config, "GALLERY_PATH", data / "gallery.npz")
    monkeypatch.setattr(config, "DB_PATH", data / "attendance.db")
    monkeypatch.setattr(config, "SNAPSHOT_DIR", data / "snapshots")
    monkeypatch.setattr(config, "RUN_DIR", data / "run")
    monkeypatch.setattr(config, "LOG_DIR", data / "logs")
    monkeypatch.setattr(config, "LIVE_DIR", data / "live")
