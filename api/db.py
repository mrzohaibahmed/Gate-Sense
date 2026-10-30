"""The API's only way to the database.

Routes never import sqlite3 or app.db directly; they go through ``Database``,
which picks a backend from ``config.DATABASE_URL`` (GATESENSE_DB_URL; None =
SQLite at ``config.DB_PATH``): SQLite, or Microsoft SQL Server for an
mssql+pyodbc:// URL (app/mssql.py, a connection that behaves like sqlite3's).
Both reuse app.db, so the API, the dashboard and the camera workers share one
schema and one set of queries (corrections and their audit rows included).
Users and sessions (Phase 2) are queried here; every change to a user writes
its audit row in the same transaction. The data endpoints (Phase 3) call
app.db's functions wherever the dashboard has one, and page events and the
audit log in SQL (the few differences: app.dbconn).

A postgresql:// URL is recognised and reported as not supported, rather than
silently using SQLite.
"""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from app import config, dbconn
from app import db as app_db

UnsupportedDatabase = dbconn.DatabaseConfigError


def ts(dt: datetime | None) -> str | None:
    """Timestamps are stored like app.db's: local time, ISO text."""
    return None if dt is None else dt.isoformat(sep=" ", timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def like(text: str) -> str:
    """A LIKE pattern matching `text` anywhere (its % and _ taken literally; use with ESCAPE '!')."""
    return "%" + text.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


USER_COLUMNS = ("id, username, full_name, email, role, password_hash, active, created_at, last_login_at, "
                "must_change_password, failed_logins, locked_until")


class Database:
    def __init__(self, url: str | None = None):
        self._url = url  # None = config.DATABASE_URL, read each time (tests change config)
        self._ready: set[Path | str] = set()  # files / URLs whose tables were checked/upgraded by this process

    @property
    def url(self) -> str:
        """The database URL (it may hold a password: show dbconn.safe_url(url) only)."""
        if self._url:
            return self._url
        dbconn.server_url()  # refuses the old variable on its own (it would otherwise mean SQLite)
        return config.DATABASE_URL or f"sqlite:///{config.DB_PATH.as_posix()}"

    @property
    def backend(self) -> str:
        """"sqlite", "mssql", "postgresql" (refused) or another URL scheme (refused)."""
        return dbconn.backend_of(self.url)

    @property
    def path(self) -> Path:
        """The SQLite file (sqlite:///relative/path is relative to the project folder)."""
        if self.backend != "sqlite" or not self.url.lower().startswith("sqlite:///"):
            raise UnsupportedDatabase(f"not a sqlite:/// URL: {self.backend}")
        path = Path(self.url[len("sqlite:///"):])
        return path if path.is_absolute() else config.ROOT_DIR / path

    def _check_supported(self) -> None:
        dbconn.check_url(self.url)  # raises for PostgreSQL and unknown URLs

    def connect(self) -> sqlite3.Connection:
        """A new connection. The first one per file (or SQL Server URL) creates or upgrades the tables, like the
        dashboard and the workers do; later ones skip that (one connection per request)."""
        self._check_supported()
        if self.backend == dbconn.MSSQL:
            from app import mssql

            url = self.url
            conn = mssql.connect(url, migrate=url not in self._ready)
            self._ready.add(url)
            return conn
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

    @contextmanager
    def conn(self):
        """A plain connection, for app.db functions that manage their own transactions."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def status(self) -> dict:
        """{"status": "ok" | "missing" | "error", "backend": ..., "detail": ...}; never creates the database."""
        try:
            self._check_supported()
            if self.backend == dbconn.MSSQL:
                from app import mssql

                return mssql.status(self.url)
            path = self.path
        except UnsupportedDatabase as e:
            try:
                backend = self.backend
            except UnsupportedDatabase:
                backend = "unknown"
            return {"status": "error", "backend": backend, "detail": str(e)}
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

    AUDIT_SORT = {"timestamp": "timestamp", "id": "id", "user": '"user"', "action": "action", "target": "target"}

    def audit_page(self, params, start: datetime | None = None, end: datetime | None = None,
                   user: str | None = None, action: str | None = None) -> tuple[list[dict], int]:
        """One page of the audit log; `end` is exclusive."""
        from api.paging import limit_offset, order_by

        where, args = [], []
        kind = self.backend
        for cond, value in (("timestamp >= ?", ts(start)), ("timestamp < ?", ts(end)), ('"user" = ?', user),
                            ("action = ?", action)):
            if value is not None:
                where.append(cond)
                args.append(value)
        if params.q:
            where.append("(" + " OR ".join(f"{dbconn.like(kind, col)} LIKE ? ESCAPE '!'"
                                           for col in ('"user"', "target", "details", "reason")) + ")")
            args += [like(params.q)] * 4
        clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
        limit, page = limit_offset(params, kind)
        order = order_by(params, self.AUDIT_SORT, "-timestamp", "id DESC")
        with self.conn() as conn:
            total = conn.execute("SELECT count(*) FROM audit_log " + clause, args).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM audit_log {clause}{order} {limit}", (*args, *page)).fetchall()
        return [dict(r) for r in rows], total

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

    def preference(self, user_id: int, key: str) -> str | None:
        with self.transaction() as conn:
            row = conn.execute('SELECT value FROM user_preferences WHERE user_id = ? AND "key" = ?',
                               (user_id, key)).fetchone()
        return row[0] if row else None

    def set_preference(self, user_id: int, key: str, value: str | None) -> None:
        with self.transaction() as conn:
            values = (user_id, key, value, datetime.now().isoformat(timespec="seconds"))
            if dbconn.is_mssql(conn):
                conn.execute(app_db._merge("user_preferences", ["user_id", "key", "value", "updated"],
                                           ["user_id", "key"], ["value", "updated"]), values)
            else:
                conn.execute('INSERT INTO user_preferences (user_id, "key", value, updated) VALUES (?, ?, ?, ?) '
                             'ON CONFLICT (user_id, "key") DO UPDATE SET value = excluded.value, '
                             "updated = excluded.updated", values)

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
                keep = () if keep_session is None else (keep_session,)
                conn.execute("DELETE FROM sessions WHERE user_id = ?" + (" AND id <> ?" if keep else ""),
                             (user_id, *keep))

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

    # --- Sites -----------------------------------------------------------------------

    def sites(self) -> list[dict]:
        with self.conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT s.id, s.name, "
                "(SELECT count(*) FROM cameras c WHERE c.site_id = s.id) AS cameras, "
                "(SELECT count(*) FROM persons p WHERE p.site_id = s.id) AS people FROM sites s")]

    def site(self, site_id: int) -> dict | None:
        return next((s for s in self.sites() if s["id"] == site_id), None)

    def create_site(self, name: str, actor: str, reason: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute("INSERT INTO sites (name) VALUES (?)", (name,))
            app_db.write_audit(conn, actor, "add_site", f"site:{name}", {}, reason)
            return cur.lastrowid

    def update_site(self, site_id: int, changes: dict, actor: str, reason: str) -> None:
        with self.transaction() as conn:
            old = dict(conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone())
            diff = {k: [old[k], v] for k, v in changes.items() if old[k] != v}
            if not diff:
                return
            sets = ", ".join(f"{k} = ?" for k in diff)
            conn.execute(f"UPDATE sites SET {sets} WHERE id = ?", (*(v[1] for v in diff.values()), site_id))
            app_db.write_audit(conn, actor, "edit_site", f"site:{old['name']}", diff, reason)

    def delete_site(self, site_id: int, actor: str, reason: str) -> None:
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
            conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
            app_db.write_audit(conn, actor, "delete_site", f"site:{old['name']}", {}, reason)

    # --- People ------------------------------------------------------------------------

    def people(self) -> list[dict]:
        with self.conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT p.id, p.employee_code, p.name, p.department, p.shift, p.active, p.site_id, "
                "s.name AS site FROM persons p LEFT JOIN sites s ON s.id = p.site_id")]

    def person(self, person_id: int) -> dict | None:
        return next((p for p in self.people() if p["id"] == person_id), None)

    def add_person(self, code: str, name: str, department: str | None, shift: str | None, site_id: int | None,
                   actor: str, reason: str) -> int:
        with self.conn() as conn:
            return app_db.add_person(conn, code, name, department, shift, reason, actor, site_id)

    def plan_import(self, data: bytes, filename: str):
        """What importing a people file would do (app.people_import.plan); changes nothing."""
        from app import people_import

        with self.conn() as conn:
            return people_import.plan(conn, data, filename)

    def import_people(self, data: bytes, filename: str, actor: str, reason: str):
        """Import a people file in one transaction, audited (app.people_import.import_people)."""
        from app import people_import

        with self.conn() as conn:
            return people_import.import_people(conn, data, filename, actor, reason)

    def update_person(self, person_id: int, name: str, department: str | None, shift: str | None,
                      site_id: int | None, actor: str, reason: str) -> None:
        with self.conn() as conn:
            app_db.update_person(conn, person_id, name, department, shift, reason, actor, site_id)

    def set_person_active(self, person_id: int, active: bool, actor: str, reason: str) -> None:
        with self.conn() as conn:
            app_db.set_person_active(conn, person_id, active, reason, actor)

    # --- Cameras (the list itself is cameras.json; the table gives ids and sites) ------------

    def cameras(self, configured: list[dict]) -> list[dict]:
        """Table rows for the configured cameras (added if missing, as the dashboard does)."""
        with self.conn() as conn:
            app_db.ensure_cameras(conn, configured)
            return [dict(r) for r in conn.execute(
                "SELECT c.id, c.name, c.direction, c.site_id, s.name AS site FROM cameras c "
                "LEFT JOIN sites s ON s.id = c.site_id")]

    def set_camera_row(self, name: str, source, direction: str, site_id: int | None) -> int:
        """Add or update the camera's table row (source stored masked, as recognize.py does)."""
        with self.conn() as conn:
            camera_id = app_db.upsert_camera(conn, name, source, direction)
            if site_id is not None:
                with conn:
                    conn.execute("UPDATE cameras SET site_id = ? WHERE id = ?", (site_id, camera_id))
            return camera_id

    def log_change(self, actor: str, action: str, target: str, details: dict, reason: str) -> None:
        with self.conn() as conn:
            app_db.log_change(conn, actor, action, target, details, reason)

    def events_by_camera(self, day: date) -> dict[int, int]:
        """Non-voided events per camera id on `day`."""
        start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
        with self.conn() as conn:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT camera_id, count(*) FROM attendance_events WHERE timestamp >= ? AND timestamp < ? "
                "AND voided = 0 GROUP BY camera_id", (start, end))}

    # --- Daily attendance: roster days, or calendar days with shifts off; the same for every screen and report ------

    def attendance_days(self, first: date, last: date, now: datetime | None = None) -> list:
        """app.db.attendance_days: roster days (shifts on) or calendar days (shifts off)."""
        with self.conn() as conn:
            return app_db.attendance_days(conn, first, last, now)

    def repeat_ids(self, rows: list[dict]) -> set[int]:
        """Which of these event rows no day uses, under the rule in use (app.db.attendance_repeat_event_ids)."""
        days = [parse_ts(r["timestamp"]).date() for r in rows if not r["voided"]]
        if not days:
            return set()
        with self.conn() as conn:
            repeats = app_db.attendance_repeat_event_ids(conn, min(days), max(days))
        return {r["id"] for r in rows if r["id"] in repeats}

    def shift_mode(self) -> dict:
        """Shifts on or off (app_settings, read on every call): {"enabled", "updated", "updated_by"}."""
        with self.conn() as conn:
            return app_db.shift_mode(conn)

    def set_shifts_enabled(self, enabled: bool, actor: str, reason: str) -> bool:
        with self.conn() as conn:
            return app_db.set_shifts_enabled(conn, enabled, reason, actor)

    # --- Shifts (effective-dated versions) and the roster ---------------------------------------------------------

    def shifts(self, on: date | None = None) -> list[dict]:
        """Every shift as in force on `on` (default today), with all its versions and how many roster days use it."""
        with self.conn() as conn:
            used = app_db.shift_roster_days(conn)
            out = []
            for shift in app_db.list_shifts(conn, on):
                out.append({"shift": shift, "roster_days": used.get(shift.id, 0),
                            "versions": [dict(r) for r in app_db.shift_version_rows(conn, shift.id)]})
            return out

    def shift(self, shift_id: int, on: date | None = None) -> dict | None:
        return next((s for s in self.shifts(on) if s["shift"].id == shift_id), None)

    def add_shift(self, shift, actor: str, reason: str) -> int:
        with self.conn() as conn:
            return app_db.add_shift(conn, shift, reason, actor)

    def change_shift(self, shift_id: int, rules, effective_from: date, actor: str, reason: str) -> bool:
        with self.conn() as conn:
            return app_db.change_shift(conn, shift_id, rules, effective_from, reason, actor)

    def rename_shift(self, shift_id: int, name: str, actor: str, reason: str) -> bool:
        with self.conn() as conn:
            return app_db.rename_shift(conn, shift_id, name, reason, actor)

    def delete_shift(self, shift_id: int, actor: str, reason: str) -> None:
        with self.conn() as conn:
            app_db.delete_shift(conn, shift_id, reason, actor)

    def delete_shift_version(self, shift_id: int, version_id: int, actor: str, reason: str) -> None:
        with self.conn() as conn:
            app_db.delete_shift_version(conn, shift_id, version_id, reason, actor)

    def roster(self, first: date, last: date) -> list[dict]:
        """Roster rows (person_id, employee_code, date, shift_id: None = OFF) of the dates `first` to `last`."""
        with self.conn() as conn:
            return [dict(r) for r in app_db.roster_rows(conn, first, last)]

    def set_roster(self, sets: list[tuple[int, date, int | None]], clears: list[tuple[int, date]], actor: str,
                   reason: str) -> tuple[int, int]:
        """Set and remove roster entries in one transaction, each change audited; (changed, removed)."""
        app_db.require_reason(reason)
        with self.transaction() as conn:
            return (app_db.write_roster(conn, sets, reason, actor),
                    app_db.clear_roster_rows(conn, clears, reason, actor))

    def plan_roster_import(self, data: bytes, filename: str):
        """What importing a roster file would do (app.roster_import.plan); changes nothing."""
        from app import roster_import

        with self.conn() as conn:
            return roster_import.plan(conn, data, filename)

    def import_roster(self, data: bytes, filename: str, actor: str, reason: str):
        """Import a roster file in one transaction, audited (app.roster_import.import_roster)."""
        from app import roster_import

        with self.conn() as conn:
            return roster_import.import_roster(conn, data, filename, actor, reason)

    def roster_template(self, first: date, last: date, kind: str, site_id: int | None, department: str | None) -> bytes:
        from app import roster_import

        with self.conn() as conn:
            return roster_import.template(conn, first, last, kind, site_id, department)

    # --- Events ----------------------------------------------------------------------------

    EVENT_SORT = {"timestamp": "e.timestamp", "id": "e.id", "name": "p.name", "employee_code": "p.employee_code",
                  "camera": "c.name", "direction": "c.direction", "score": "e.match_score"}

    def events(self, params, start: datetime | None = None, end: datetime | None = None,
               camera_id: int | None = None, direction: str | None = None,
               person_id: int | None = None) -> tuple[list[dict], int]:
        """One page of raw events (manual and voided ones included); `end` is exclusive."""
        from api.paging import limit_offset, order_by

        where, args = [], []
        for cond, value in (("e.timestamp >= ?", ts(start)), ("e.timestamp < ?", ts(end)),
                            ("e.camera_id = ?", camera_id), ("c.direction = ?", direction),
                            ("e.person_id = ?", person_id), ("c.site_id = ?", params.site_id)):
            if value is not None:
                where.append(cond)
                args.append(value)
        kind = self.backend
        if params.q:
            where.append("(" + " OR ".join(f"{dbconn.like(kind, col)} LIKE ? ESCAPE '!'"
                                           for col in ("p.name", "p.employee_code", "c.name")) + ")")
            args += [like(params.q)] * 3
        clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
        limit, page = limit_offset(params, kind)
        order = order_by(params, self.EVENT_SORT, "-timestamp", "e.id DESC")
        with self.conn() as conn:
            total = conn.execute("SELECT count(*) FROM attendance_events e JOIN persons p ON p.id = e.person_id "
                                 "JOIN cameras c ON c.id = e.camera_id " + clause, args).fetchone()[0]
            rows = conn.execute(f"{app_db.EVENT_COLUMNS}{clause}{order} {limit}", (*args, *page)).fetchall()
        return [dict(r) for r in rows], total

    def event(self, event_id: int) -> dict | None:
        with self.conn() as conn:
            row = conn.execute(app_db.EVENT_COLUMNS + "WHERE e.id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def recent_events(self, person_id: int, since: date) -> list[dict]:
        with self.conn() as conn:
            return [dict(r) for r in conn.execute(
                app_db.EVENT_COLUMNS + "WHERE e.person_id = ? AND e.timestamp >= ? ORDER BY e.timestamp DESC",
                (person_id, since.isoformat()))]

    def add_manual_event(self, person_id: int, camera_id: int, when: datetime, actor: str, reason: str) -> int:
        with self.conn() as conn:
            return app_db.add_manual_event(conn, person_id, camera_id, when, reason, actor)

    def edit_event_time(self, event_id: int, when: datetime, actor: str, reason: str) -> None:
        with self.conn() as conn:
            app_db.edit_event_time(conn, event_id, when, reason, actor)

    def set_event_voided(self, event_id: int, voided: bool, actor: str, reason: str) -> None:
        with self.conn() as conn:
            app_db.set_event_voided(conn, event_id, voided, reason, actor)

    # --- Live updates (/api/ws) ----------------------------------------------------------

    def last_ids(self) -> tuple[int, int]:
        """(newest event id, newest audit id): where a new WebSocket feed starts."""
        with self.conn() as conn:
            return (conn.execute("SELECT COALESCE(MAX(id), 0) FROM attendance_events").fetchone()[0],
                    conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0])

    def events_after(self, event_id: int, limit: int = 200) -> list[dict]:
        """Events added since `event_id` (by the camera workers or as corrections), oldest first."""
        with self.conn() as conn:
            return [dict(r) for r in conn.execute(app_db.EVENT_COLUMNS + "WHERE e.id > ? ORDER BY e.id"
                                                  + dbconn.limit(conn), (event_id, limit))]

    def audit_after(self, audit_id: int, actions: tuple[str, ...], limit: int = 200) -> tuple[list[dict], int]:
        """Audit rows with one of `actions` since `audit_id` (from the API or the dashboard), and the newest id seen."""
        with self.conn() as conn:
            newest = conn.execute("SELECT COALESCE(MAX(id), ?) FROM audit_log WHERE id > ?",
                                  (audit_id, audit_id)).fetchone()[0]
            marks = ", ".join("?" * len(actions))
            rows = conn.execute(f"SELECT * FROM audit_log WHERE id > ? AND id <= ? AND action IN ({marks}) "
                                "ORDER BY id" + dbconn.limit(conn), (audit_id, newest, *actions, limit)).fetchall()
        return [dict(r) for r in rows], newest

    def sessions_alive(self, session_ids: list[int], now: datetime) -> set[int]:
        """Which of these sessions still exist, haven't expired and belong to an active user."""
        if not session_ids:
            return set()
        marks = ", ".join("?" * len(session_ids))
        with self.conn() as conn:
            return {r[0] for r in conn.execute(
                f"SELECT s.id FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id IN ({marks}) "
                "AND s.expires_at > ? AND u.active = 1", (*session_ids, ts(now)))}
