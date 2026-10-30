"""Which database GateSense uses, and the few pieces of SQL that differ between SQLite and SQL Server.

config.DATABASE_URL (GATESENSE_DB_URL, from the environment only) unset = SQLite at config.DB_PATH: the sqlite3
connection exactly as before. An mssql+pyodbc:// URL = Microsoft SQL Server through SQLAlchemy's engine and pyodbc
(app/mssql.py), whose connection behaves like sqlite3's for app/db.py: execute(sql with ? marks), rows by name and
by position, `with conn:` commits, BEGIN IMMEDIATE, lastrowid, rowcount, one writer at a time. Values come back the
way SQLite returns them (times as ISO text, flags as 0/1), so the attendance rules, audit details and API answers
are the same on both.

The URL holds a password: show it only through safe_url().
"""

import re
import sqlite3

from app import config

try:
    import pyodbc
except ImportError:  # a PC that only uses SQLite
    pyodbc = None
try:
    from sqlalchemy.exc import SQLAlchemyError
except ImportError:
    SQLAlchemyError = None

SQLITE, MSSQL = "sqlite", "mssql"
# SQL Server collations: text compares and sorts like SQLite's default (case-sensitive, by code point); the two
# COLLATE NOCASE columns (users.username, shifts.name) and LIKE searches (SQLite's LIKE ignores case) use CI.
CS = "Latin1_General_100_BIN2"
CI = "Latin1_General_100_CI_AS"

# Every error a database call can raise, on either backend (a worker keeps its events and retries on these).
ERRORS: tuple[type[BaseException], ...] = tuple(
    e for e in (sqlite3.Error, pyodbc and pyodbc.Error, SQLAlchemyError) if e is not None)
INTEGRITY_ERRORS = tuple(e for e in (sqlite3.IntegrityError, pyodbc and pyodbc.IntegrityError) if e is not None)


class DatabaseConfigError(RuntimeError):
    """The database setting can't be used (unknown or old variable, wrong URL, a schema this version can't run)."""


def backend_of(url: str) -> str:
    """"sqlite", "mssql", "postgresql" or the URL's scheme as it is."""
    scheme = url.split(":", 1)[0].lower()
    if scheme in ("postgres", "postgresql") or scheme.startswith("postgresql+"):
        return "postgresql"
    if scheme == "mssql" or scheme.startswith("mssql+"):
        return MSSQL
    if scheme == "sqlite" or scheme.startswith("sqlite+"):
        return SQLITE
    return scheme


def server_url() -> str | None:
    """The SQL Server URL in use, or None for SQLite. Raises DatabaseConfigError for a URL GateSense can't use,
    and when only the old variable is set (it would otherwise silently fall back to SQLite)."""
    url = config.DATABASE_URL
    if not url:
        if config.OLD_DATABASE_URL:
            raise DatabaseConfigError(
                f"{config.OLD_DATABASE_URL_ENV} is set, but GateSense now reads {config.DATABASE_URL_ENV}: set "
                f"{config.DATABASE_URL_ENV} instead (and remove {config.OLD_DATABASE_URL_ENV}), or remove it to use "
                "SQLite")
        return None
    return check_url(url)


def check_url(url: str) -> str | None:
    """`url` if it is a SQL Server URL GateSense can use, None for SQLite; DatabaseConfigError otherwise."""
    kind = backend_of(url)
    if kind == MSSQL:
        if not url.lower().startswith("mssql+pyodbc://"):
            raise DatabaseConfigError(f"{config.DATABASE_URL_ENV}: SQL Server URLs must start with mssql+pyodbc://")
        if pyodbc is None or SQLAlchemyError is None:
            raise DatabaseConfigError("SQL Server needs pyodbc and SQLAlchemy: install requirements.txt")
        return url
    if kind == SQLITE:
        return None
    if kind == "postgresql":
        raise DatabaseConfigError(f"PostgreSQL is not supported yet: set {config.DATABASE_URL_ENV} to an "
                                  "mssql+pyodbc:// URL, or remove it to use data/attendance.db")
    raise DatabaseConfigError(f"unknown database type {kind!r} in {config.DATABASE_URL_ENV}")


def safe_url(url: str) -> str:
    """The URL with its password replaced by ***, for messages and logs."""
    try:
        from sqlalchemy import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001  not a URL SQLAlchemy reads: hide anything between : and @
        return re.sub(r"(://[^:/@]*:)[^@]*@", r"\1***@", url)


def dialect(conn) -> str:
    """SQLITE for a sqlite3 connection, MSSQL for app.mssql.Connection."""
    return getattr(conn, "dialect", SQLITE)


def is_mssql(conn) -> bool:
    return dialect(conn) == MSSQL


def limit(conn) -> str:
    """The end of a query that has an ORDER BY, keeping its first ? rows."""
    return " OFFSET 0 ROWS FETCH NEXT ? ROWS ONLY" if is_mssql(conn) else " LIMIT ?"


def page(kind: str, size: int, offset: int) -> tuple[str, tuple[int, int]]:
    """The end of an ordered query for one page, and its parameters, for backend `kind`."""
    if kind == MSSQL:
        return "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", (offset, size)
    return "LIMIT ? OFFSET ?", (size, offset)


def like(kind: str, column: str) -> str:
    """`column` for a LIKE that ignores case, like SQLite's (SQL Server columns compare case-sensitively)."""
    return f"{column} COLLATE {CI}" if kind == MSSQL else column
