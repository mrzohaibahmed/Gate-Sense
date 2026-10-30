"""Copy GateSense's SQLite database into SQL Server (GATESENSE_DB_URL), keeping every id, in one transaction.

    .venv\\Scripts\\python scripts\\migrate_sqlite_to_mssql.py                 # data\\attendance.db -> GATESENSE_DB_URL
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_mssql.py --sqlite D:\\old\\attendance.db
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_mssql.py --force         # the SQL Server tables have rows: empty
                                                                             # them first (same transaction)
    .venv\\Scripts\\python scripts\\migrate_sqlite_to_mssql.py --check         # compare only; writes nothing

What it does:
1. Refuses while GateSense or a camera worker runs (nothing may be written to SQLite after the copy).
2. Opens the SQLite file read-only and checks it: PRAGMA integrity_check and foreign_key_check, the schema version
   GateSense runs (an older file: start GateSense on it once, it upgrades with a copy kept), the expected columns.
3. Checks that every value fits SQL Server unchanged: times (to the millisecond, no time zones), dates, "HH:MM"
   times, 0/1 flags, numbers, the length of indexed text, and no two keys that SQL Server would see as the same
   (trailing spaces; capitals in users.username and shifts.name). It never cuts or rewrites a value: it refuses
   and lists the rows instead.
4. In one SQL Server transaction, holding GateSense's write lock: creates the tables if there are none (without
   the default site and shifts: they come from SQLite), refuses if any table has rows (--force: empties them
   first), copies every table with its ids (IDENTITY_INSERT), reads everything back and compares it with SQLite
   row by row. Any difference rolls the whole copy back.
5. Prints the row counts of both databases per table, each table's checksum (SHA-256 of its rows), and the face
   gallery's SHA-256 before and after (the embeddings stay in data\\gallery.npz; this never touches it).

The SQLite file is never changed or deleted: to go back to SQLite, remove GATESENSE_DB_URL. The URL's password is
never printed.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time as clock
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Integer  # noqa: E402
from sqlalchemy.dialects.mssql import BIT, DATE, DATETIME2, FLOAT, NVARCHAR, TIME  # noqa: E402

from api.workers import api_pid  # noqa: E402
from app import config, db, dbconn, mssql  # noqa: E402

import restore  # noqa: E402  scripts/restore.py, next to this file (running_workers)

CHUNK = 1000  # rows per insert batch


class Refused(Exception):
    """The migration can't run (or found a difference); nothing was written."""


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def gallery_summary(path: Path) -> str:
    if not path.is_file():
        return "no gallery.npz (no one enrolled)"
    from app.gallery import LOAD_ERRORS, Gallery

    try:
        counts = Gallery.load(path).counts()
    except LOAD_ERRORS as e:
        return f"can't be read: {e}"
    return f"{len(counts)} people, {sum(counts.values())} embeddings"


# --- Columns and values -------------------------------------------------------------------------------------------


def kind(column) -> str:
    t = column.type
    for cls, name in ((DATETIME2, "datetime"), (DATE, "date"), (TIME, "time"), (BIT, "bit"), (FLOAT, "float"),
                      (NVARCHAR, "text"), (Integer, "int")):
        if isinstance(t, cls):
            return name
    raise TypeError(f"{column}: no conversion for {t!r}")


def text_units(value: str) -> int:
    """NVARCHAR length: UTF-16 code units."""
    return len(value.encode("utf-16-le")) // 2


def convert(table: str, column, value, problems: list[str], notes: dict, row_id) -> tuple:
    """(value to insert into SQL Server, the value as GateSense reads it back) for one SQLite value. Problems are
    appended (nothing is ever changed to make it fit); `notes` counts values that change only in form."""
    where = f"{table}.{column.name} (row {row_id})"
    if value is None:
        if not column.nullable:
            problems.append(f"{where}: empty, but SQL Server requires a value")
        return None, None
    k = kind(column)
    if k == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{where}: {value!r} is not a whole number")
            return None, None
        return value, value
    if k == "bit":
        if value not in (0, 1) or not isinstance(value, int):
            problems.append(f"{where}: {value!r} is not 0 or 1")
            return None, None
        return int(value), int(value)
    if k == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(f"{where}: {value!r} is not a number")
            return None, None
        return float(value), float(value)
    if not isinstance(value, str):
        problems.append(f"{where}: {value!r} is not text")
        return None, None
    if k == "text":
        length = column.type.length
        if length is not None and text_units(value) > length:
            problems.append(f"{where}: {text_units(value)} characters, at most {length} fit")
        return value, value
    try:
        if k == "datetime":
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                raise ValueError("has a time zone")
            if parsed.microsecond % 1000:
                raise ValueError("finer than a millisecond")
        elif k == "date":
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError("not YYYY-MM-DD")
        else:
            parsed = time.fromisoformat(value)
            if parsed.tzinfo is not None or parsed.microsecond:
                raise ValueError("not HH:MM")
    except ValueError as e:
        problems.append(f"{where}: {value!r} can't be stored unchanged ({e})")
        return None, None
    shown = mssql.sqlite_value(parsed)
    if shown != value:  # e.g. "2026-10-29T08:00:00": the same time, written as GateSense writes it
        notes[f"{table}.{column.name}"] = notes.get(f"{table}.{column.name}", 0) + 1
    return parsed, shown


def key_of(table, row: dict, columns: list) -> tuple:
    """How SQL Server compares a unique key: trailing spaces don't count; NOCASE columns ignore capitals."""
    out = []
    for c in columns:
        v = row[c.name]
        if isinstance(v, str):
            v = v.rstrip(" ")
            if getattr(c.type, "collation", None) == dbconn.CI:
                v = v.casefold()
        out.append(v)
    return tuple(out)


def unique_keys(table) -> list[list]:
    keys = [list(table.primary_key.columns)]
    keys += [list(c.columns) for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]
    keys += [list(i.columns) for i in table.indexes if i.unique]
    return [k for k in keys if k]


def quote(name: str) -> str:
    return f'"{name}"'


# --- SQLite side -------------------------------------------------------------------------------------------------


def read_sqlite(path: Path) -> dict[str, list[dict]]:
    """Every table's rows, read in one read transaction (a consistent picture), after the file's own checks."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        integrity = "; ".join(str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()[:5])
        if integrity != "ok":
            raise Refused(f"the SQLite file fails PRAGMA integrity_check ({integrity}); restore a backup first")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != len(db.VERSIONS):
            raise Refused(f"the SQLite file has schema version {version}; this GateSense runs {len(db.VERSIONS)}. "
                          + ("Start GateSense once on it (it upgrades, keeping a copy), then run this again."
                             if version < len(db.VERSIONS) else "Use the GateSense version that made it."))
        if broken := conn.execute("PRAGMA foreign_key_check").fetchall()[:5]:
            raise Refused("the SQLite file has rows pointing at missing rows (PRAGMA foreign_key_check): "
                          + ", ".join(f"{r[0]} row {r[1]} -> {r[2]}" for r in broken))
        conn.execute("BEGIN")
        out = {}
        for name in mssql.TABLES:
            table = mssql.metadata.tables[name]
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({name})")}
            want = {c.name for c in table.columns}
            if have != want:
                raise Refused(f"SQLite table {name} has columns {sorted(have)}, expected {sorted(want)}")
            cols = ", ".join(quote(c.name) for c in table.columns)
            out[name] = [dict(r) for r in conn.execute(f"SELECT {cols} FROM {name}")]
        conn.rollback()
    return out


def prepare(rows: dict[str, list[dict]]) -> tuple[dict, dict, list[str], dict]:
    """(values to insert per table, the rows as GateSense reads them back per table, problems, notes)."""
    inserts, expected, problems, notes = {}, {}, [], {}
    for name in mssql.TABLES:
        table = mssql.metadata.tables[name]
        columns = list(table.columns)
        pk = [c.name for c in table.primary_key.columns]
        inserts[name], expected[name] = [], []
        for row in rows[name]:
            row_id = row["id"] if "id" in row else tuple(row[c] for c in pk)
            pairs = [convert(name, c, row[c.name], problems, notes, row_id) for c in columns]
            inserts[name].append(tuple(p[0] for p in pairs))
            expected[name].append(tuple(p[1] for p in pairs))
        for key in unique_keys(table):
            seen: dict[tuple, object] = {}
            for row in rows[name]:
                k = key_of(table, row, key)
                if any(v is None for v in k):
                    continue  # NULLs never clash (SQLite's rule, kept by the filtered index)
                if k in seen:
                    problems.append(f"{name}: {[c.name for c in key]} = {[row[c.name] for c in key]} and "
                                    f"{list(seen[k])} would be the same key in SQL Server")
                seen[k] = [row[c.name] for c in key]
    return inserts, expected, problems, notes


def ordered(name: str, rows: list[tuple]) -> list[tuple]:
    """Rows in primary-key order (the same order on both sides, whatever the collation)."""
    table = mssql.metadata.tables[name]
    positions = [list(table.columns).index(c) for c in table.primary_key.columns]
    return sorted(rows, key=lambda r: tuple(r[i] for i in positions))


def checksum(rows: list[tuple]) -> str:
    return hashlib.sha256(json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


# --- SQL Server side -----------------------------------------------------------------------------------------------


def read_server(conn: mssql.Connection) -> dict[str, list[tuple]]:
    out = {}
    for name in mssql.TABLES:
        cols = ", ".join(quote(c.name) for c in mssql.metadata.tables[name].columns)
        out[name] = [tuple(r) for r in conn.execute(f"SELECT {cols} FROM {name}")]
    return out


def counts(conn: mssql.Connection) -> dict[str, int]:
    return {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in mssql.TABLES}


def copy(conn: mssql.Connection, inserts: dict[str, list[tuple]]) -> None:
    for name in mssql.TABLES:
        table = mssql.metadata.tables[name]
        if not inserts[name]:
            continue
        cols = [c.name for c in table.columns]
        sql = (f"INSERT INTO {name} ({', '.join(map(quote, cols))}) "
               f"VALUES ({', '.join('?' * len(cols))})")
        identity = "id" in cols
        if identity:
            conn.execute(f"SET IDENTITY_INSERT {name} ON")
        for start in range(0, len(inserts[name]), CHUNK):
            conn.executemany(sql, inserts[name][start:start + CHUNK])
        if identity:
            conn.execute(f"SET IDENTITY_INSERT {name} OFF")
        print(f"  copied {name}: {len(inserts[name])} rows", flush=True)


def compare(expected: dict[str, list[tuple]], actual: dict[str, list[tuple]]) -> dict[str, tuple[str, str]]:
    """Each table's (SQLite checksum, SQL Server checksum); Refused at the first row that differs."""
    out = {}
    for name in mssql.TABLES:
        want, got = ordered(name, expected[name]), ordered(name, actual[name])
        if len(want) != len(got):
            raise Refused(f"{name}: {len(want)} rows in SQLite, {len(got)} in SQL Server")
        for a, b in zip(want, got):
            if a != b:
                raise Refused(f"{name}: a row differs after the copy:\n  SQLite:     {a}\n  SQL Server: {b}")
        out[name] = (checksum(want), checksum(got))
    return out


def report(sqlite_counts: dict, server_counts: dict, sums: dict | None) -> bool:
    print(f"\n{'table':<18} {'SQLite':>10} {'SQL Server':>11}  checksum (SHA-256 of the rows)")
    ok = True
    for name in mssql.TABLES:
        same = sqlite_counts[name] == server_counts[name]
        line = f"{name:<18} {sqlite_counts[name]:>10} {server_counts[name]:>11}  "
        if sums:
            a, b = sums[name]
            same = same and a == b
            line += f"{'match' if a == b else 'DIFFERENT'}  {a[:16]}" + ("" if a == b else f" / {b[:16]}")
        ok = ok and same
        print(line + ("" if same else "   <-- differs"))
    print(f"{'total':<18} {sum(sqlite_counts.values()):>10} {sum(server_counts.values()):>11}")
    return ok


# --- The run -------------------------------------------------------------------------------------------------------


def running() -> str | None:
    if pid := api_pid():
        return f"GateSense is running (process {pid})"
    if names := restore.running_workers():
        return f"camera workers are running: {', '.join(names)}"
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts\\migrate_sqlite_to_mssql.py",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", type=Path, default=None, help=f"the SQLite file (default {config.DB_PATH})")
    parser.add_argument("--force", action="store_true",
                        help="empty the SQL Server tables first if they have rows (in the same transaction)")
    parser.add_argument("--check", action="store_true",
                        help="only compare SQLite with SQL Server (counts and checksums); writes nothing")
    args = parser.parse_args(argv)

    try:
        url = dbconn.server_url()
    except dbconn.DatabaseConfigError as e:
        print(f"ERROR: {e}")
        return 1
    if not url:
        print(f"ERROR: set {config.DATABASE_URL_ENV} to the SQL Server database (mssql+pyodbc://...) first.")
        return 1
    source = (args.sqlite or config.DB_PATH).resolve()
    print(f"SQLite:     {source}")
    print(f"SQL Server: {dbconn.safe_url(url)}")
    if not source.is_file():
        print(f"ERROR: no SQLite database at {source}")
        return 1
    if problem := running():
        print(f"ERROR: {problem}. Stop GateSense first (stop.bat), so nothing is written to SQLite after the copy.")
        return 1

    sqlite_sha, gallery_sha = sha256(source), sha256(config.GALLERY_PATH)
    print(f"gallery.npz before: {gallery_sha or '-'}  ({gallery_summary(config.GALLERY_PATH)})")
    started = clock.monotonic()
    try:
        rows = read_sqlite(source)
        inserts, expected, problems, notes = prepare(rows)
        if problems:
            raise Refused(f"{len(problems)} value(s) can't be stored in SQL Server unchanged:\n  "
                          + "\n  ".join(problems[:50]) + ("\n  ..." if len(problems) > 50 else ""))
        sqlite_counts = {name: len(rows[name]) for name in mssql.TABLES}
        try:
            conn = mssql.Connection(url, query_timeout=0)
        except dbconn.ERRORS as e:
            raise Refused(f"can't connect to SQL Server: {mssql.first_line(e)}") from None
        with closing(conn):
            if args.check:
                if mssql.schema_version(conn) is None:
                    raise Refused("SQL Server has no GateSense tables yet: nothing to compare")
                server = read_server(conn)
                ok = report(sqlite_counts, {n: len(server[n]) for n in mssql.TABLES}, compare_quietly(expected, server))
                print("\nSQLite and SQL Server hold the same data." if ok else "\nThey DIFFER (see above).")
                return 0 if ok else 1
            conn.begin()  # GateSense's write lock: no GateSense process writes while this runs
            try:
                version = mssql.schema_version(conn)
                if version is None:
                    mssql.create_tables(conn, seed=False)
                    print("  created the tables")
                elif version != mssql.SCHEMA_VERSION:
                    raise Refused(f"the SQL Server database has schema version {version}, this GateSense "
                                  f"{mssql.SCHEMA_VERSION}")
                before = counts(conn)
                if any(before.values()):
                    filled = ", ".join(f"{n} {c}" for n, c in before.items() if c)
                    if not args.force:
                        raise Refused(f"the SQL Server tables already have rows ({filled}). Nothing was changed. "
                                      "Run with --force to empty them and copy again (in one transaction).")
                    for name in reversed(mssql.TABLES):
                        conn.execute(f"DELETE FROM {name}")
                    print(f"  --force: emptied the SQL Server tables ({filled})")
                copy(conn, inserts)
                sums = compare(expected, read_server(conn))
                server_counts = counts(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except Refused as e:
        print(f"ERROR: {e}")
        print("Nothing was written to SQL Server." if not args.check else "")
        return 1
    except dbconn.ERRORS as e:
        print(f"ERROR from the database: {mssql.first_line(e)}")
        print("The SQL Server transaction was rolled back: nothing was written.")
        return 1

    print(f"\nCopied and committed in {clock.monotonic() - started:.1f} s.")
    ok = report(sqlite_counts, server_counts, sums)
    for column, n in sorted(notes.items()):
        print(f"note: {column}: {n} value(s) were written in another form in SQLite (e.g. with 'T'); the same "
              "times, now shown as GateSense writes them")
    print(f"\nSQLite file unchanged: {'yes' if sha256(source) == sqlite_sha else 'NO (it changed during the run!)'}"
          f"  ({source})")
    gallery_after = sha256(config.GALLERY_PATH)
    print(f"gallery.npz after:  {gallery_after or '-'}  "
          f"({'untouched' if gallery_after == gallery_sha else 'CHANGED during the run!'})")
    print("\nDone. GateSense uses SQL Server whenever GATESENSE_DB_URL is set; remove it to go back to SQLite."
          if ok else "\nThe counts differ (see above).")
    return 0 if ok else 1


def compare_quietly(expected: dict, server: dict) -> dict:
    """compare() for --check, where a difference is a result to show, not an error."""
    out = {}
    for name in mssql.TABLES:
        want, got = ordered(name, expected[name]), ordered(name, server[name])
        out[name] = (checksum(want), checksum(got))
    return out


if __name__ == "__main__":
    sys.exit(main())
