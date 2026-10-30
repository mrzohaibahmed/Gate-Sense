import os

import pytest

from app import config

SHIPPED_SHIFTS_DEFAULT = config.SHIFTS_ENABLED_DEFAULT  # before any test changes it

# SQL Server run: GATESENSE_TEST_DB_URL = an mssql+pyodbc:// URL of a separate, disposable database (its name must
# contain "test"); every test starts with it empty. Never GATESENSE_DB_URL: that is the real attendance database.
TEST_DB_URL = os.environ.get("GATESENSE_TEST_DB_URL") or None


def _check_test_database(url: str) -> None:
    from app import dbconn, mssql

    if dbconn.backend_of(url) != dbconn.MSSQL:
        pytest.exit("GATESENSE_TEST_DB_URL must be an mssql+pyodbc:// URL", 2)
    name = mssql.database_name(url)
    real = os.environ.get(config.DATABASE_URL_ENV)
    if "test" not in name.lower() or (real and mssql.database_name(real).lower() == name.lower()):
        pytest.exit(f"GATESENSE_TEST_DB_URL's database {name!r} must be a separate test database (its name "
                    f"containing 'test', not the one in {config.DATABASE_URL_ENV}): the tests empty it", 2)


if TEST_DB_URL:
    _check_test_database(TEST_DB_URL)


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: tests of SQLite itself (files, PRAGMAs, schema steps); "
                                       "skipped on the SQL Server run")


def pytest_collection_modifyitems(config, items):
    if TEST_DB_URL:
        skip = pytest.mark.skip(reason="SQLite only (GATESENSE_TEST_DB_URL is set)")
        for item in items:
            if "sqlite_only" in item.keywords:
                item.add_marker(skip)


def empty_test_database(url: str) -> None:
    """Drop GateSense's tables: the next connection creates them, with the defaults, as for a new install."""
    from app import mssql

    conn = mssql.Connection(url)
    try:
        conn.begin()
        for table in reversed([*mssql.TABLES, mssql.VERSION_TABLE]):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never touch the real .env, known/ or data/, and undo any variable a test loads from a .env file."""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "no-such.env")
    # setenv-then-delenv records the original state, so teardown also removes a value load_dotenv set.
    monkeypatch.setenv(config.DASHBOARD_PASSWORD_ENV, "placeholder")
    monkeypatch.delenv(config.DASHBOARD_PASSWORD_ENV)

    # The database: the test's own SQLite file, or the emptied test database on the SQL Server run. Never the real
    # GATESENSE_DB_URL, here or in a worker a test starts (they inherit the environment).
    monkeypatch.setattr(config, "OLD_DATABASE_URL", None)
    monkeypatch.delenv(config.OLD_DATABASE_URL_ENV, raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DB_URL)
    if TEST_DB_URL:
        monkeypatch.setenv(config.DATABASE_URL_ENV, TEST_DB_URL)
        empty_test_database(TEST_DB_URL)
    else:
        monkeypatch.delenv(config.DATABASE_URL_ENV, raising=False)

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
    # Shifts are off by default (SHIPPED_SHIFTS_DEFAULT); most tests exercise the shift rules, so they start with shifts
    # on. tests/test_shift_mode.py checks the shipped default and the calendar days.
    monkeypatch.setattr(config, "SHIFTS_ENABLED_DEFAULT", True)
    # Never the real liveness models, and never a download: tests inject a fake (tests/fakes.py FakeLiveness).
    from app import liveness

    monkeypatch.setattr(config, "LIVENESS_MODEL_DIR", tmp_path / "models")

    def no_download(url, dest):
        raise OSError("no downloads in tests")

    monkeypatch.setattr(liveness, "fetch", no_download)
    liveness.shared.cache_clear()
    # The models' device: the CPU, whatever this PC has (tests/test_device.py checks the real one).
    from app import models

    monkeypatch.setattr(models, "_device", models.Device("cpu", "auto"))
