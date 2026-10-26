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
    # The real cameras.json (if any) must not leak into tests: start from the defaults every time.
    monkeypatch.setattr(config, "CAMERAS_FILE", tmp_path / "cameras.json")
    monkeypatch.setattr(config, "CAMERAS", [dict(c) for c in config.DEFAULT_CAMERAS])
    monkeypatch.setattr(config, "_cameras_signature", None)  # matches the (missing) temporary cameras.json
    monkeypatch.setattr(config, "CAMERAS_ERROR", None)  # even if the real cameras.json can't be read
    # Likewise a real settings.json: tests start from the values written in app/config.py.
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_settings_signature", None)
    for name, value in config.DEFAULT_SETTINGS.items():
        monkeypatch.setattr(config, name, value)
    # Tests log in many times a minute from one address: a fresh, generous login rate limit for each test
    # (tests/test_production.py checks the real one). The web build is never the real web/dist.
    from api import auth

    monkeypatch.setattr(config, "LOGIN_RATE_LIMIT", 10_000)
    monkeypatch.setattr(auth, "login_limit", auth.RateLimit())
    monkeypatch.setattr(config, "WEB_DIST", tmp_path / "no-web-dist")
    monkeypatch.setattr(config, "MAINTENANCE_CHECK_SECONDS", 3600)
    monkeypatch.setattr(config, "BACKUP_COPY_DIR", None)  # never a real second backup folder
    monkeypatch.setattr(config, "HEALTH_MIN_FREE_BYTES", 0)  # the health checks don't depend on this PC's free space
    # Never the real liveness models, and never a download: tests inject a fake (tests/fakes.py FakeLiveness).
    from app import liveness

    monkeypatch.setattr(config, "LIVENESS_MODEL_DIR", tmp_path / "models")

    def no_download(url, dest):
        raise OSError("no downloads in tests")

    monkeypatch.setattr(liveness, "fetch", no_download)
    liveness.shared.cache_clear()
