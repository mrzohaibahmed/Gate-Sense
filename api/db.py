"""The API's only way to the database.

Routes never import sqlite3 or app.db directly; they go through ``Database``,
which picks a backend from ``config.DATABASE_URL`` (None = SQLite at
``config.DB_PATH``). The SQLite backend reuses app.db, so the API, the
dashboard and the camera workers share one schema and one set of queries
(corrections and their audit rows included). Users and sessions (Phase 2)
are queried here; every change to a user writes its audit row in the same
transaction.

Moving to PostgreSQL later means adding a backend here (and porting app.db's
queries, which the camera workers use too); the API code stays as it is and
the switch is the URL. A postgresql:// URL is recognised now and reported as
not supported, rather than silently using SQLite.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app import config
from app import db as app_db


class UnsupportedDatabase(RuntimeError):
    pass


def ts(dt: datetime | None) -> str | None:
    """Timestamps are stored like app.db's: local time, ISO text."""
    return None if dt is None else dt.isoformat(sep=" ", timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


USER_COLUMNS = ("id, username, full_name, email, role, password_hash, active, created_at, last_login_at, "
                "must_change_password, failed_logins, locked_until")


class Database:
    def __init__(self, url: str | None = None):
        self._url = url  # None = config.DATABASE_URL, read each time (tests change config)
        self._ready: set[Path] = set()  # files whose tables were checked/upgraded by this process

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
        """A new connection. The first one per file creates or upgrades the tables, like the dashboard and the
        workers do; later ones skip that (one connection per request)."""
        self._check_supported()
        path = self.path
        conn = app_db.connect(path, migrate=path not in self._ready or not path.is_file())
        self._ready.add(path)
        return conn

    @contextmanager
    def transaction(self):
        """A connection whose changes are committed together at the end (rolled back on an error)."""
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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

    # --- Audit ---------------------------------------------------------------------

    def audit(self, user: str, action: str, target: str, details: dict, reason: str) -> None:
        with self.transaction() as conn:
            app_db.write_audit(conn, user, action, target, details, reason)

    def audit_entries(self, limit: int = 500) -> list[dict]:
        with self.transaction() as conn:
            return [dict(r) for r in app_db.audit_entries(conn, limit)]

    # --- Users ---------------------------------------------------------------------

    def count_users(self) -> int:
        with self.transaction() as conn:
            return conn.execute("SELECT count(*) FROM users").fetchone()[0]

    def user(self, user_id: int | None = None, username: str | None = None) -> dict | None:
        with self.transaction() as conn:
            if user_id is not None:
                row = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
            else:
                row = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def users(self) -> list[dict]:
        with self.transaction() as conn:
            return [dict(r) for r in conn.execute(f"SELECT {USER_COLUMNS} FROM users ORDER BY username")]

    def active_admins(self) -> int:
        with self.transaction() as conn:
            return conn.execute("SELECT count(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]

    def create_user(self, user: dict, actor: str, reason: str) -> int:
        """`user`: username, full_name, email, role, password_hash, must_change_password, created_at."""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, full_name, email, role, password_hash, must_change_password, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user["username"], user["full_name"], user["email"], user["role"], user["password_hash"],
                 int(user["must_change_password"]), ts(user["created_at"])))
            details = {k: user[k] for k in ("full_name", "email", "role")}
            app_db.write_audit(conn, actor, "create_user", f"user:{user['username']}", details, reason)
            return cur.lastrowid

    def update_user(self, user_id: int, changes: dict, audit: tuple[str, str, dict, str] | None = None,
                    end_sessions: bool = False, keep_session: int | None = None) -> None:
        """Set columns in `changes`; optionally write `audit` = (actor, action, details, reason) and end the
        user's sessions (all, or all but `keep_session`), in one transaction."""
        sets = ", ".join(f"{col} = ?" for col in changes)
        values = [ts(v) if isinstance(v, datetime) else v for v in changes.values()]
        with self.transaction() as conn:
            username = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()["username"]
            if changes:
                conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*values, user_id))
            if audit:
                actor, action, details, reason = audit
                app_db.write_audit(conn, actor, action, f"user:{username}", details, reason)
            if end_sessions:
                conn.execute("DELETE FROM sessions WHERE user_id = ? AND id IS NOT ?", (user_id, keep_session))

    # --- Sessions ------------------------------------------------------------------

    def create_session(self, user_id: int, token_hash: str, created: datetime, expires: datetime,
                       ip: str | None, user_agent: str | None, login_changes: dict, audit: tuple) -> int:
        """New session, the user's login bookkeeping and the login audit row, together."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (ts(created),))
            cur = conn.execute(
                "INSERT INTO sessions (user_id, token_hash, created_at, expires_at, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?)", (user_id, token_hash, ts(created), ts(expires), ip, user_agent))
            sets = ", ".join(f"{col} = ?" for col in login_changes)
            values = [ts(v) if isinstance(v, datetime) else v for v in login_changes.values()]
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*values, user_id))
            app_db.write_audit(conn, *audit)
            return cur.lastrowid

    def session(self, token_hash: str) -> dict | None:
        """The session and its user (as user_* columns), or None."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT s.id, s.user_id, s.created_at, s.expires_at, u.username, u.full_name, u.email, u.role, "
                "u.active, u.must_change_password FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def extend_session(self, session_id: int, expires: datetime) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (ts(expires), session_id))

    def end_session(self, session_id: int, audit: tuple | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if audit:
                app_db.write_audit(conn, *audit)

    def session_count(self, user_id: int) -> int:
        with self.transaction() as conn:
            return conn.execute("SELECT count(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0]
