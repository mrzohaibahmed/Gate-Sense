"""Which database GateSense uses, and the few pieces of SQL used in several places.

GateSense stores its data in Microsoft SQL Server: config.DATABASE_URL (GATESENSE_DB_URL, from the environment or .env),
an mssql+pyodbc:// URL, opened through SQLAlchemy's engine and pyodbc (app/mssql.py). Without it GateSense refuses to
start.

The URL holds a password: show it only through safe_url().
"""

import re

import pyodbc
from sqlalchemy.exc import SQLAlchemyError

from app import config

MSSQL = "mssql"
# SQL Server collations: text compares and sorts case-sensitively, by code point; the two case-insensitive columns
# (users.username, shifts.name) and LIKE searches use CI.
CS = "Latin1_General_100_BIN2"
CI = "Latin1_General_100_CI_AS"

# Every error a database call can raise (a worker keeps its events and retries on these).
ERRORS: tuple[type[BaseException], ...] = (pyodbc.Error, SQLAlchemyError)
INTEGRITY_ERRORS = (pyodbc.IntegrityError,)
# The end of a query that has an ORDER BY, keeping its first ? rows.
LIMIT = " OFFSET 0 ROWS FETCH NEXT ? ROWS ONLY"


class DatabaseConfigError(RuntimeError):
    """The database setting can't be used (missing, old variable, wrong URL, a schema this version can't run)."""


def server_url() -> str:
    """The SQL Server URL in use. Raises DatabaseConfigError when it is missing or GateSense can't use it."""
    url = config.DATABASE_URL
    if url:
        return check_url(url)
    if config.OLD_DATABASE_URL:
        raise DatabaseConfigError(
            f"{config.OLD_DATABASE_URL_ENV} is set, but GateSense now reads {config.DATABASE_URL_ENV}: set "
            f"{config.DATABASE_URL_ENV} instead and remove {config.OLD_DATABASE_URL_ENV}")
    raise DatabaseConfigError(f"{config.DATABASE_URL_ENV} is not set: GateSense stores its data in SQL Server. Set it "
                              'to an mssql+pyodbc:// URL (see README.md, "SQL Server")')


def check_url(url: str) -> str:
    """`url` if it is a SQL Server URL GateSense can use; DatabaseConfigError otherwise."""
    scheme = url.split(":", 1)[0].lower()
    if scheme == "mssql+pyodbc":
        return url
    if scheme == "mssql" or scheme.startswith("mssql+"):
        raise DatabaseConfigError(f"{config.DATABASE_URL_ENV}: SQL Server URLs must start with mssql+pyodbc://")
    if scheme in ("postgres", "postgresql") or scheme.startswith("postgresql+"):
        raise DatabaseConfigError(f"PostgreSQL is not supported yet: set {config.DATABASE_URL_ENV} to an "
                                  "mssql+pyodbc:// URL")
    raise DatabaseConfigError(f"unknown database type {scheme!r} in {config.DATABASE_URL_ENV}: GateSense needs an "
                              "mssql+pyodbc:// URL")


def safe_url(url: str) -> str:
    """The URL with its password replaced by ***, for messages and logs."""
    try:
        from sqlalchemy import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001  not a URL SQLAlchemy reads: hide anything between : and @
        return re.sub(r"(://[^:/@]*:)[^@]*@", r"\1***@", url)


def page(size: int, offset: int) -> tuple[str, tuple[int, int]]:
    """The end of an ordered query for one page, and its parameters."""
    return "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", (offset, size)


def like(column: str) -> str:
    """`column` for a LIKE that ignores case (columns compare case-sensitively)."""
    return f"{column} COLLATE {CI}"
