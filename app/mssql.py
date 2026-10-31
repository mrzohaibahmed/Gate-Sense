"""Microsoft SQL Server: the engine, a connection that behaves like sqlite3's, and the schema.

Connection (what app.db.connect returns for an mssql+pyodbc:// URL) keeps app/db.py's SQL and its transactions as
they are on SQLite:

- execute(sql, params) with ? marks; every result is read at once (the driver allows one open result per
  connection), so rows can be used after the next execute. Rows work by name and by position, and dict(row).
- Values come back as SQLite returns them: DATETIME2 as "YYYY-MM-DD HH:MM:SS" (".fff" only when there are
  milliseconds), DATE as "YYYY-MM-DD", TIME as "HH:MM", BIT as 0/1. So the attendance rules, the audit details
  and the API answers don't depend on the backend.
- Like sqlite3: no transaction for reads; the first INSERT/UPDATE/DELETE/MERGE (or BEGIN IMMEDIATE) starts one,
  `with conn:` commits it (or rolls it back on an error). lastrowid after an INSERT, rowcount after a change.
- One writer at a time, as SQLite: every transaction first takes the exclusive application lock WRITE_LOCK
  (sp_getapplock, released with the transaction), waiting at most DB_LOCK_TIMEOUT_MS ("database is locked").
  With READ_COMMITTED_SNAPSHOT on (as SQLite's WAL) readers never wait for it.
- A lost connection (SQL Server restarted, network) is dropped; the next statement outside a transaction gets a
  new one from the pool (pre-pinged), at most once every DB_RECONNECT_SECONDS for a long-lived connection, so a
  camera worker isn't held up by a server that is down. The transaction the loss interrupted is gone (the server
  rolled it back, or, if the loss came during COMMIT, maybe committed it: see app.db.add_camera_event's retry).

The schema is the SQLite schema at version SCHEMA_VERSION (len(app.db.VERSIONS)) in SQL Server types: IDENTITY
keys, DATETIME2(3) times, DATE, TIME(0), BIT flags, FLOAT scores, NVARCHAR text (bounded only where indexed), the
same unique constraints and indexes, and the version in gatesense_schema. Schema steps after it need a SQL Server
version too (tests/test_mssql.py checks that the two schemas match). A database made at an older version is brought up
to date at the first connection (upgrade_schema: the UPGRADES steps, which only add tables, in one transaction under
the write lock).
"""

import threading
import time as clock
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pyodbc
from sqlalchemy import (
    CheckConstraint, Column, ForeignKey, Identity, Index, Integer, MetaData, PrimaryKeyConstraint, Table,
    UniqueConstraint, create_engine, make_url, text,
)
from sqlalchemy.dialects.mssql import BIT, DATE, DATETIME2, FLOAT, NVARCHAR, TIME
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateIndex, CreateTable

from app import config
from app.dbconn import CI, CS, MSSQL, DatabaseConfigError, safe_url

SCHEMA_VERSION = 8  # = len(app.db.VERSIONS)
WRITE_LOCK = "gatesense-write"
VERSION_TABLE = "gatesense_schema"

# --- Schema --------------------------------------------------------------------------------------------------------

metadata = MetaData(naming_convention={
    "pk": "pk_%(table_name)s", "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s"})


def _text(length: int | None = None, nocase: bool = False) -> NVARCHAR:
    """NVARCHAR(length), or NVARCHAR(MAX) when not indexed (nothing is ever cut short)."""
    return NVARCHAR(length, collation=CI if nocase else CS)


def _id() -> Column:
    return Column("id", Integer, Identity(), primary_key=True)


def _time() -> DATETIME2:
    return DATETIME2(precision=3)


Table("sites", metadata, _id(), Column("name", _text(200), nullable=False, unique=True))
Table("persons", metadata, _id(),
      Column("employee_code", _text(100), nullable=False, unique=True),
      Column("name", _text(), nullable=False),
      Column("department", _text()),
      Column("shift", _text()),
      Column("active", BIT, nullable=False, server_default=text("1")),
      Column("site_id", Integer, ForeignKey("sites.id")))
Table("cameras", metadata, _id(),
      Column("name", _text(100), nullable=False, unique=True),
      Column("source", _text(), nullable=False),
      Column("direction", _text(3), nullable=False),
      Column("site_id", Integer, ForeignKey("sites.id")),
      CheckConstraint("direction IN ('IN', 'OUT')", name="direction"))
Table("attendance_events", metadata, _id(),
      Column("person_id", Integer, ForeignKey("persons.id"), nullable=False),
      Column("camera_id", Integer, ForeignKey("cameras.id"), nullable=False),
      Column("timestamp", _time(), nullable=False),
      Column("match_score", FLOAT(53), nullable=False),
      Column("snapshot_path", _text()),
      Column("manual", BIT, nullable=False, server_default=text("0")),
      Column("voided", BIT, nullable=False, server_default=text("0")),
      Column("original_timestamp", _time()),
      Index("idx_events_timestamp", "timestamp"))
Table("audit_log", metadata, _id(),
      Column("timestamp", _time(), nullable=False),
      Column("user", _text(), nullable=False),
      Column("action", _text(), nullable=False),
      Column("target", _text(), nullable=False),
      Column("details", _text(), nullable=False),
      Column("reason", _text(), nullable=False))
Table("users", metadata, _id(),
      Column("username", _text(100, nocase=True), nullable=False, unique=True),
      Column("full_name", _text(), nullable=False, server_default=text("N''")),
      Column("email", _text()),
      Column("role", _text(), nullable=False),
      Column("password_hash", _text(), nullable=False),
      Column("active", BIT, nullable=False, server_default=text("1")),
      Column("created_at", _time(), nullable=False),
      Column("last_login_at", _time()),
      Column("must_change_password", BIT, nullable=False, server_default=text("0")),
      Column("failed_logins", Integer, nullable=False, server_default=text("0")),
      Column("locked_until", _time()))
Table("sessions", metadata, _id(),
      Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
      Column("token_hash", _text(128), nullable=False, unique=True),
      Column("created_at", _time(), nullable=False),
      Column("expires_at", _time(), nullable=False),
      Column("ip", _text()),
      Column("user_agent", _text()),
      Index("idx_sessions_user", "user_id"))
Table("user_preferences", metadata,
      Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
      Column("key", _text(100), nullable=False),
      Column("value", _text()),
      Column("updated", _time(), nullable=False),
      PrimaryKeyConstraint("user_id", "key"))
Table("shifts", metadata, _id(), Column("name", _text(100, nocase=True), nullable=False, unique=True))
Table("roster", metadata, _id(),
      Column("person_id", Integer, ForeignKey("persons.id"), nullable=False),
      Column("date", DATE, nullable=False),
      Column("shift_id", Integer, ForeignKey("shifts.id")),
      Column("updated", _time(), nullable=False),
      Column("updated_by", _text(), nullable=False),
      UniqueConstraint("person_id", "date"),
      Index("idx_roster_date", "date"))
Table("shift_versions", metadata, _id(),
      Column("shift_id", Integer, ForeignKey("shifts.id"), nullable=False),
      Column("effective_from", DATE),
      Column("start_time", TIME(precision=0), nullable=False),
      Column("end_time", TIME(precision=0), nullable=False),
      Column("crosses_midnight", BIT, nullable=False),
      Column("grace_min", Integer, nullable=False),
      Column("in_window_before_min", Integer, nullable=False),
      Column("out_window_after_min", Integer, nullable=False),
      Column("created", _time(), nullable=False),
      Column("created_by", _text(), nullable=False),
      # SQLite's UNIQUE (shift_id, effective_from) lets NULLs repeat; a filtered unique index does the same here.
      Index("uq_shift_versions_shift_id_effective_from", "shift_id", "effective_from", unique=True,
            mssql_where=text("effective_from IS NOT NULL")))
Table("app_settings", metadata,
      Column("key", _text(100), primary_key=True),
      Column("value", _text(), nullable=False),
      Column("updated", _time(), nullable=False),
      Column("updated_by", _text(), nullable=False))
Table("unknown_visits", metadata, _id(),
      Column("camera_id", Integer, ForeignKey("cameras.id"), nullable=False),
      Column("first_seen", _time(), nullable=False),
      Column("last_seen", _time(), nullable=False),
      Column("tracks", Integer, nullable=False, server_default=text("1")),
      Column("best_score", FLOAT(53), nullable=False),
      Column("closest_code", _text(100)),
      Column("liveness", FLOAT(53)),
      Column("face_px", Integer),
      Column("snapshot_path", _text()),
      Column("status", _text(20), nullable=False, server_default=text("N'new'")),
      Column("person_id", Integer, ForeignKey("persons.id")),
      Column("note", _text()),
      Column("reviewed", _time()),
      Column("reviewed_by", _text()),
      CheckConstraint("status IN ('new', 'dismissed', 'identified')", name="status"),
      Index("idx_unknown_first_seen", "first_seen"))
Table(VERSION_TABLE, metadata, Column("version", Integer, nullable=False))

TABLES = [t.name for t in metadata.sorted_tables if t.name != VERSION_TABLE]  # parents before children
# The tables each schema step after the first SQL Server schema (version 7) adds: upgrade_schema creates them.
UPGRADES = {8: ["unknown_visits"]}


# --- Engine and connection -----------------------------------------------------------------------------------------

_engines: dict[str, object] = {}
_engines_lock = threading.Lock()


def engine(url: str):
    """One pooled engine per URL per process (connections pre-pinged on checkout, so a restarted server is
    noticed before a statement fails)."""
    with _engines_lock:
        if url not in _engines:
            _engines[url] = create_engine(url, pool_pre_ping=True, pool_recycle=1800, pool_size=5,
                                          max_overflow=30, connect_args={"timeout": config.DB_LOGIN_TIMEOUT_SECONDS})
        return _engines[url]


def database_name(url: str) -> str:
    return make_url(url).database or ""


def sqlite_value(v):
    """A value as SQLite would return it (see the module docstring)."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="milliseconds" if v.microsecond else "seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return f"{v:%H:%M}" if not v.second and not v.microsecond else v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


class Row:
    """A row like sqlite3.Row: row[0], row["name"] (names ignore case, as sqlite3's), keys(), dict(row)."""

    __slots__ = ("_index", "_values")

    def __init__(self, index: dict[str, int], values: tuple):
        self._index, self._values = index, values

    def keys(self) -> list[str]:
        return [k for k in self._index if not k.startswith("\0")]

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._index[key]]
        except KeyError:
            return self._values[self._index["\0" + key.lower()]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other) -> bool:
        return tuple(self) == tuple(other) if isinstance(other, (Row, tuple)) else NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self.keys(), self._values))})"


class Cursor:
    """What execute() returns: the rows (read already), rowcount and lastrowid."""

    def __init__(self, description=None, rows=(), rowcount: int = -1, lastrowid: int | None = None):
        self.description, self.rowcount, self.lastrowid = description, rowcount, lastrowid
        index: dict[str, int] = {}
        for i, d in enumerate(description or ()):
            index.setdefault(d[0], i)
            index.setdefault("\0" + d[0].lower(), i)
        self._rows = [Row(index, tuple(sqlite_value(v) for v in r)) for r in rows]
        self._next = 0

    def fetchone(self) -> Row | None:
        if self._next >= len(self._rows):
            return None
        self._next += 1
        return self._rows[self._next - 1]

    def fetchall(self) -> list[Row]:
        rest, self._next = self._rows[self._next:], len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


WRITES = ("INSERT", "UPDATE", "DELETE", "MERGE")


def _locked(message: str) -> pyodbc.OperationalError:
    return pyodbc.OperationalError("GSLCK", message)


class Connection:
    """A SQL Server connection that behaves like sqlite3's for app/db.py (see the module docstring)."""

    dialect = MSSQL

    def __init__(self, url: str, query_timeout: int | None = None):
        self.url = url
        self._engine = engine(url)
        self._fairy = None  # SQLAlchemy's pooled DBAPI connection
        self.in_transaction = False
        self.closed = False
        self.query_timeout = config.DB_QUERY_TIMEOUT_SECONDS if query_timeout is None else query_timeout
        self._retry_at = 0.0
        self._dbapi()  # like sqlite3.connect: a server that can't be reached fails here

    # sqlite3's interface ---------------------------------------------------------------------------------------

    def execute(self, sql: str, params=()) -> Cursor:
        if self.closed:
            raise pyodbc.ProgrammingError("Cannot operate on a closed database.")
        word = (sql.split(None, 1) or [""])[0].upper()
        if word == "BEGIN":
            if self.in_transaction:
                raise pyodbc.ProgrammingError("cannot start a transaction within a transaction")
            self.begin()
            return Cursor()
        insert = word == "INSERT"
        if word in WRITES and not self.in_transaction:
            self.begin()
        if insert:
            sql = sql.rstrip().rstrip(";") + "; SELECT CAST(SCOPE_IDENTITY() AS INT)"
        results = self._run(sql, tuple(params))
        if insert:
            (_, count), (_, rows) = results[0], results[-1]
            return Cursor(rowcount=count, lastrowid=rows[0][0] if rows else None)
        for description, rows in results:
            if description is not None:
                return Cursor(description, rows)
        return Cursor(rowcount=results[-1][1] if results else -1)

    def commit(self) -> None:
        if not self.in_transaction:
            return
        try:
            self._run("COMMIT TRANSACTION", ())
        finally:
            self.in_transaction = False

    def rollback(self) -> None:
        if not self.in_transaction:
            return
        self.in_transaction = False
        if self._fairy is None:
            return  # the connection is gone, and the server rolled the transaction back with it
        try:
            self._run("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION", ())
        except pyodbc.Error:
            self._drop()  # never hand a connection with an open transaction back to the pool

    def close(self) -> None:
        if self.closed:
            return
        self.rollback()
        if self._fairy is not None:
            self._fairy.close()  # back to the pool
            self._fairy = None
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            self.commit()
        else:
            self.rollback()
        return False

    # The rest ------------------------------------------------------------------------------------------------

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """One statement for many rows (the migration's copy), inside the transaction (started if needed)."""
        if not self.in_transaction:
            self.begin()
        cur = self._dbapi().cursor()
        try:
            cur.fast_executemany = True
            cur.executemany(sql, rows)
        except pyodbc.Error as e:
            if self._engine.dialect.is_disconnect(e, self._fairy, cur):
                self._drop()
            raise
        finally:
            try:
                cur.close()
            except pyodbc.Error:
                pass

    def begin(self) -> None:
        """Start a transaction holding the write lock (one writer at a time, as SQLite's BEGIN IMMEDIATE)."""
        self._run("BEGIN TRANSACTION", ())
        self.in_transaction = True
        try:
            results = self._run(
                f"DECLARE @r int; EXEC @r = sp_getapplock @Resource = N'{WRITE_LOCK}', @LockMode = 'Exclusive', "
                f"@LockOwner = 'Transaction', @LockTimeout = {int(config.DB_LOCK_TIMEOUT_MS)}; SELECT @r", ())
            granted = next(rows for description, rows in results if description is not None)[0][0]
            if granted < 0:
                raise _locked(f"database is locked: another GateSense process held the write lock for over "
                              f"{config.DB_LOCK_TIMEOUT_MS / 1000:g} s (sp_getapplock {granted})")
        except BaseException:
            self.rollback()
            raise

    def _dbapi(self):
        if self._fairy is None:
            wait = self._retry_at - clock.monotonic()
            if wait > 0:
                raise pyodbc.OperationalError("08001", f"not connected to SQL Server; trying again in {wait:.0f} s")
            try:
                fairy = self._engine.raw_connection()
            except DBAPIError as e:
                self._retry_at = clock.monotonic() + config.DB_RECONNECT_SECONDS
                raise e.orig from None
            fairy.driver_connection.autocommit = True  # transactions are explicit (begin/commit/rollback)
            fairy.driver_connection.timeout = self.query_timeout
            self._fairy = fairy
        return self._fairy.driver_connection

    def _run(self, sql: str, params: tuple) -> list[tuple]:
        """Run one batch; every result set (description, rows) or row count (None, count), in order."""
        cur = self._dbapi().cursor()
        try:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            results = []
            while True:
                results.append((cur.description, cur.fetchall()) if cur.description is not None
                               else (None, cur.rowcount))
                if not cur.nextset():
                    break
            return results
        except pyodbc.Error as e:
            if self._engine.dialect.is_disconnect(e, self._fairy, cur):
                self._drop()
            raise
        finally:
            try:
                cur.close()
            except pyodbc.Error:
                pass

    def _drop(self) -> None:
        """Forget a broken connection (closed, never reused); its transaction is gone with it."""
        self.in_transaction = False
        if self._fairy is not None:
            try:
                self._fairy.invalidate()
            except Exception:  # noqa: BLE001  already broken
                pass
            self._fairy = None
        self._retry_at = clock.monotonic() + config.DB_RECONNECT_SECONDS


# --- Schema version, creating the tables -----------------------------------------------------------------------


def _object_exists(conn: Connection, name: str) -> bool:
    return conn.execute("SELECT OBJECT_ID(?, N'U')", (f"dbo.{name}",)).fetchone()[0] is not None


def existing_tables(conn: Connection) -> list[str]:
    """GateSense's tables that exist in the database."""
    rows = conn.execute("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID(N'dbo')").fetchall()
    names = {r[0].lower() for r in rows}
    return [t for t in [*TABLES, VERSION_TABLE] if t.lower() in names]


def schema_version(conn: Connection) -> int | None:
    """The schema version, or None if the database has no GateSense schema yet."""
    if not _object_exists(conn, VERSION_TABLE):
        return None
    row = conn.execute(f"SELECT MAX(version) FROM {VERSION_TABLE}").fetchone()
    return row[0]


def _ddl(conn: Connection, names: list[str] | None = None) -> list[str]:
    """CREATE TABLE and CREATE INDEX for every table (or only `names`), parents first."""
    dialect = conn._engine.dialect
    tables = [t for t in metadata.sorted_tables if names is None or t.name in names]
    out = [str(CreateTable(t).compile(dialect=dialect)).strip() for t in tables]
    out += [str(CreateIndex(i).compile(dialect=dialect)).strip() for t in tables for i in t.indexes]
    return out


def upgradable(version: int) -> bool:
    """Can a database at `version` be brought up to SCHEMA_VERSION (an UPGRADES step for every version after it)?"""
    return version < SCHEMA_VERSION and all(v in UPGRADES for v in range(version + 1, SCHEMA_VERSION + 1))


def upgrade_schema(conn: Connection) -> int | None:
    """Run the UPGRADES steps a database made at an older version hasn't had, in one transaction under the write
    lock (two processes starting at once upgrade it once). Returns the version it had, or None if nothing ran."""
    conn.begin()
    try:
        version = schema_version(conn)
        if version is None or version >= SCHEMA_VERSION:
            conn.rollback()
            return None
        if not upgradable(version):
            raise DatabaseConfigError(f"the SQL Server database has schema version {version}; this GateSense runs "
                                      f"version {SCHEMA_VERSION} and has no SQL Server upgrade step from it")
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _ddl(conn, UPGRADES[step]):
                conn.execute(statement)
        conn.execute(f"UPDATE {VERSION_TABLE} SET version = ?", (SCHEMA_VERSION,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    print(f"SQL Server database {database_name(conn.url)} upgraded from schema version {version} to "
          f"{SCHEMA_VERSION}", flush=True)
    return version


def create_schema(conn: Connection, seed: bool) -> bool:
    """Create the tables at SCHEMA_VERSION in an empty database, in one transaction under the write lock (two
    processes starting at once create them once). `seed`: also the default site and config.DEFAULT_SHIFTS, as a
    new SQLite database gets (the migration copies its own). False if the schema was already there."""
    conn.begin()
    try:
        if schema_version(conn) is not None:
            conn.rollback()
            return False
        create_tables(conn, seed)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


def create_tables(conn: Connection, seed: bool) -> None:
    """The tables at SCHEMA_VERSION, inside the caller's transaction (see create_schema); refused if some of
    GateSense's tables are there already."""
    if found := existing_tables(conn):
        raise DatabaseConfigError(f"the database has GateSense tables ({', '.join(found)}) but no "
                                  f"{VERSION_TABLE}: it was set up by hand or half-way; use an empty database")
    for statement in _ddl(conn):
        conn.execute(statement)
    conn.execute(f"INSERT INTO {VERSION_TABLE} (version) VALUES (?)", (SCHEMA_VERSION,))
    if seed:
        _seed(conn)


def _seed(conn: Connection) -> None:
    """What schema steps 1, 5 and 6 put into a new SQLite database: the default site and the default shifts, each
    with its first version (from the start)."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    conn.execute("INSERT INTO sites (name) VALUES (?)", (config.DEFAULT_SITE_NAME,))
    for s in config.DEFAULT_SHIFTS:
        shift_id = conn.execute("INSERT INTO shifts (name) VALUES (?)", (s["name"],)).lastrowid
        conn.execute("INSERT INTO shift_versions (shift_id, effective_from, start_time, end_time, crosses_midnight, "
                     "grace_min, in_window_before_min, out_window_after_min, created, created_by) "
                     "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 'system')",
                     (shift_id, s["start"], s["end"], int(s["crosses_midnight"]), config.SHIFT_GRACE_MIN,
                      config.SHIFT_IN_WINDOW_BEFORE_MIN, config.SHIFT_OUT_WINDOW_AFTER_MIN, now))


def check_version(conn: Connection, allow_upgrade: bool = False) -> None:
    """Raise DatabaseConfigError unless the schema is the one this version of GateSense runs (`allow_upgrade`: or
    an older one upgrade_schema can bring up to date, as the first connection will)."""
    version = schema_version(conn)
    if version is None:
        if found := existing_tables(conn):
            raise DatabaseConfigError(f"the database has GateSense tables ({', '.join(found)}) but no "
                                      f"{VERSION_TABLE}: use an empty database")
        return
    if allow_upgrade and upgradable(version):
        return
    if version != SCHEMA_VERSION:
        raise DatabaseConfigError(
            f"the SQL Server database has schema version {version}; this GateSense runs version {SCHEMA_VERSION}"
            + (" (it is newer than this GateSense: update GateSense)" if version > SCHEMA_VERSION else
               " (no SQL Server upgrade step is written for it)"))


def connect(url: str, migrate: bool = True) -> Connection:
    """A connection; with `migrate`, the tables are created (empty database) or their version checked first."""
    conn = Connection(url)
    if migrate:
        try:
            version = schema_version(conn)
            if version is None:
                create_schema(conn, seed=True)
            elif version < SCHEMA_VERSION:
                upgrade_schema(conn)
            check_version(conn)
        except BaseException:
            conn.close()
            raise
    return conn


def read_committed_snapshot(conn: Connection) -> bool:
    row = conn.execute("SELECT is_read_committed_snapshot_on FROM sys.databases WHERE database_id = DB_ID()").fetchone()
    return bool(row and row[0])


def startup_problem(url: str) -> str | None:
    """Why GateSense must not start on this SQL Server database (can't connect, a schema it can't run), or None.
    An empty database is fine: the tables are created at the start."""
    try:
        conn = Connection(url)
    except pyodbc.Error as e:
        return f"can't connect to SQL Server ({safe_url(url)}): {first_line(e)}"
    try:
        check_version(conn, allow_upgrade=True)  # an older schema is upgraded when the app first connects
        if not read_committed_snapshot(conn):
            print(f"WARNING: READ_COMMITTED_SNAPSHOT is off for database {database_name(url)}: readers wait for "
                  "writers. Turn it on (see README).", flush=True)
    except DatabaseConfigError as e:
        return str(e)
    except pyodbc.Error as e:
        return f"SQL Server ({safe_url(url)}): {first_line(e)}"
    finally:
        conn.close()
    return None


def status(url: str) -> dict:
    """For /api/health: {"status": "ok" | "error", "backend": "mssql", "detail": ...}; never creates anything."""
    try:
        conn = Connection(url)
        try:
            tables = len(existing_tables(conn))
        finally:
            conn.close()
    except pyodbc.Error as e:
        return {"status": "error", "backend": MSSQL, "detail": first_line(e)}
    if not tables:
        return {"status": "missing", "backend": MSSQL,
                "detail": "no tables yet: they are created when the dashboard or a camera first starts"}
    return {"status": "ok", "backend": MSSQL, "detail": f"{tables} tables"}


def database_bytes(conn: Connection) -> int:
    """The database's data and log files."""
    return conn.execute("SELECT CAST(SUM(CAST(size AS BIGINT)) * 8192 AS BIGINT) FROM sys.database_files"
                        ).fetchone()[0] or 0


def first_line(e: Exception) -> str:
    """The driver's message, without the [Microsoft][ODBC Driver ...] prefixes (never includes the password)."""
    message = str(e.args[1] if len(e.args) > 1 else e)
    return message.replace("[Microsoft]", "").replace("[ODBC Driver 18 for SQL Server]", "").replace(
        "[ODBC Driver 17 for SQL Server]", "").replace("[SQL Server]", "").strip().splitlines()[0][:300]


# --- Backups (api/maintenance.py) -------------------------------------------------------------------------------


def backup_database(url: str, path: Path) -> None:
    """A full copy-only backup to `path`, written by the SQL Server service (its account needs write access to
    the folder), with page checksums. Consistent while the cameras keep writing; breaks no backup chain."""
    conn = Connection(url, query_timeout=0)
    try:
        conn.execute("DECLARE @db sysname = DB_NAME(); "
                     "BACKUP DATABASE @db TO DISK = ? WITH COPY_ONLY, CHECKSUM, INIT, FORMAT", (str(path),))
    finally:
        conn.close()


def verify_backup(url: str, path: Path) -> str:
    """RESTORE VERIFYONLY of a backup file (complete, readable, checksums right): "ok" or the problem."""
    try:
        conn = Connection(url, query_timeout=0)
    except pyodbc.Error as e:
        return f"can't connect to SQL Server: {first_line(e)}"
    try:
        conn.execute("RESTORE VERIFYONLY FROM DISK = ? WITH CHECKSUM", (str(path),))
    except pyodbc.Error as e:
        return first_line(e)
    finally:
        conn.close()
    return "ok"


def restore_commands(url: str, path: Path) -> str:
    """The T-SQL an administrator runs to put a backup file back (the app's login can't replace its database)."""
    name = database_name(url).replace("]", "]]")
    file = str(path).replace("'", "''")
    return (f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;\n"
            f"RESTORE DATABASE [{name}] FROM DISK = N'{file}' WITH REPLACE, CHECKSUM, RECOVERY;\n"
            f"ALTER DATABASE [{name}] SET MULTI_USER;")
