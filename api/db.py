"""The API's only way to the database.

Routes never import sqlite3 or app.db directly; they go through ``Database``,
which picks a backend from ``config.DATABASE_URL`` (None = SQLite at
``config.DB_PATH``). The SQLite backend reuses app.db, so the API, the
dashboard and the camera workers share one schema and one set of queries
(corrections and their audit rows included).

Moving to PostgreSQL later means adding a backend here (and porting app.db's
queries, which the camera workers use too); the API code stays as it is and
the switch is the URL. A postgresql:// URL is recognised now and reported as
not supported, rather than silently using SQLite.
"""

import sqlite3
from pathlib import Path

from app import config
from app import db as app_db


class UnsupportedDatabase(RuntimeError):
    pass


class Database:
    def __init__(self, url: str | None = None):
        self._url = url  # None = config.DATABASE_URL, read each time (tests change config)

    @property
    def url(self) -> str:
        return self._url or config.DATABASE_URL or f"sqlite:///{config.DB_PATH.as_posix()}"

    @property
    def backend(self) -> str:
        scheme = self.url.split(":", 1)[0].lower()
        return "postgresql" if scheme in ("postgres", "postgresql") or scheme.startswith("postgresql+") else scheme

    @property
    def path(self) -> Path:
        """The SQLite file (sqlite:///relative/path is relative to the project folder)."""
        if self.backend != "sqlite" or not self.url.lower().startswith("sqlite:///"):
            raise UnsupportedDatabase(f"not a sqlite:/// URL: {self.backend}")
        path = Path(self.url[len("sqlite:///"):])
        return path if path.is_absolute() else config.ROOT_DIR / path

    def _check_supported(self) -> None:
        if self.backend == "postgresql":
            raise UnsupportedDatabase("PostgreSQL is not supported yet: set GATESENSE_DATABASE_URL to a sqlite:/// "
                                      "URL, or leave it unset to use data/attendance.db")
        if self.backend != "sqlite":
            raise UnsupportedDatabase(f"unknown database type {self.backend!r} in the database URL")

    def connect(self) -> sqlite3.Connection:
        """A new connection (creates or upgrades the tables, like the dashboard and the workers do)."""
        self._check_supported()
        return app_db.connect(self.path)

    def status(self) -> dict:
        """{"status": "ok" | "missing" | "error", "backend": ..., "detail": ...}; never creates the database."""
        try:
            self._check_supported()
            path = self.path
        except UnsupportedDatabase as e:
            return {"status": "error", "backend": self.backend, "detail": str(e)}
        if not path.is_file():
            return {"status": "missing", "backend": "sqlite",
                    "detail": "no database yet: it is created when the dashboard or a camera first starts"}
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            try:
                tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            return {"status": "error", "backend": "sqlite", "detail": str(e)}
        return {"status": "ok", "backend": "sqlite", "detail": f"{tables} tables"}
