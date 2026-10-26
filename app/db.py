"""SQLite storage for people, cameras and attendance (data/attendance.db).

Tables follow PLAN.md. Embeddings stay in data/gallery.npz for now, so there is
no face_embeddings table yet. ``cameras.source`` replaces PLAN.md's
``rtsp_url`` because a source can also be a webcam index or a video file.

Manual corrections never delete anything: an admin can add a manual event,
change an event's time, or void (and restore) an event. Every change, and
every change to a person, writes an ``audit_log`` row with a required reason
in the same transaction. Voided events are ignored by the daily summary.
Manual events have ``manual = 1`` and ``match_score = 0``.

Timestamps are local time stored as ISO text ("YYYY-MM-DD HH:MM:SS"). WAL mode
lets one recognize.py per camera and the dashboard use the database at once.

Schema changes after the first version are numbered steps (VERSIONS), tracked
in PRAGMA user_version and run by connect() in one transaction. They only add
tables, columns and rows; nothing existing is changed or deleted. Before the
first step runs on a database that has data, a copy goes to backups/ next to it.
Step 1 adds the API's users, sessions and sites (every camera in one default site).
Step 2 gives people a site too, and events an original_timestamp: the time an
event had before its first correction (edit_event_time keeps it). Step 3 adds
user_preferences. Step 4 is the one step that removes: the daily_attendance table
(a copy of the summaries that nothing read; they are computed from the events)
and sites.timezone (stored but never applied: a day is local midnight to midnight).
The usual backup copy is made first, so the old values are kept there. Step 5 adds
shifts (seeded with config.DEFAULT_SHIFTS) and the roster (person, date, shift or
NULL = OFF; one row per person per date). Roster days are computed from the raw
events by app/shifts.py, like the calendar days, so nothing derived is stored.
"""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app import config
from app.attendance import DaySummary, Event, repeat_ids, summarize_day, within_cooldown
from app.camera import mask_source
from app.shifts import OFF, ROSTER_PAD_DAYS, Shift, ShiftDay, roster_repeat_ids, summarize_roster

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id            INTEGER PRIMARY KEY,
    employee_code TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    department    TEXT,
    shift         TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cameras (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    source    TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('IN', 'OUT'))
);
CREATE TABLE IF NOT EXISTS attendance_events (
    id            INTEGER PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES persons(id),
    camera_id     INTEGER NOT NULL REFERENCES cameras(id),
    timestamp     TEXT NOT NULL,
    match_score   REAL NOT NULL,
    snapshot_path TEXT,
    manual        INTEGER NOT NULL DEFAULT 0,
    voided        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON attendance_events(timestamp);
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    user      TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL,
    details   TEXT NOT NULL,
    reason    TEXT NOT NULL
);
"""

# Columns added after the first schema; connect() adds them to older databases.
MIGRATIONS = {
    "attendance_events": {
        "manual": "INTEGER NOT NULL DEFAULT 0",
        "voided": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(sep=" ", timespec="seconds")


DEFAULT_SITE = "(SELECT MIN(id) FROM sites)"  # SQL for the default site's id


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _v1_users_sessions_sites(conn: sqlite3.Connection) -> None:
    """API login (Phase 2): users, sessions, sites; cameras.site_id, existing cameras in one default site.

    Roles are not constrained here: the list lives in api/permissions.py, so adding one needs no table rebuild.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS sites (
        id       INTEGER PRIMARY KEY,
        name     TEXT NOT NULL UNIQUE,
        timezone TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id                   INTEGER PRIMARY KEY,
        username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
        full_name            TEXT NOT NULL DEFAULT '',
        email                TEXT,
        role                 TEXT NOT NULL,
        password_hash        TEXT NOT NULL,
        active               INTEGER NOT NULL DEFAULT 1,
        created_at           TEXT NOT NULL,
        last_login_at        TEXT,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        failed_logins        INTEGER NOT NULL DEFAULT 0,
        locked_until         TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        ip         TEXT,
        user_agent TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    if conn.execute("SELECT 1 FROM sites LIMIT 1").fetchone() is None:
        conn.execute("INSERT INTO sites (name, timezone) VALUES (?, 'local')", (config.DEFAULT_SITE_NAME,))
    if "site_id" not in _columns(conn, "cameras"):
        # Nullable: SQLite only adds a REFERENCES column with a NULL default; filled in just below.
        conn.execute("ALTER TABLE cameras ADD COLUMN site_id INTEGER REFERENCES sites(id)")
    conn.execute(f"UPDATE cameras SET site_id = {DEFAULT_SITE} WHERE site_id IS NULL")


def _v2_person_sites_original_times(conn: sqlite3.Connection) -> None:
    """Data endpoints (Phase 3): persons.site_id (existing people in the default site) and
    attendance_events.original_timestamp (set when a correction first changes an event's time)."""
    if "site_id" not in _columns(conn, "persons"):
        conn.execute("ALTER TABLE persons ADD COLUMN site_id INTEGER REFERENCES sites(id)")
    conn.execute(f"UPDATE persons SET site_id = {DEFAULT_SITE} WHERE site_id IS NULL")
    if "original_timestamp" not in _columns(conn, "attendance_events"):
        conn.execute("ALTER TABLE attendance_events ADD COLUMN original_timestamp TEXT")


def _v3_user_preferences(conn: sqlite3.Connection) -> None:
    """Per-user preferences (e.g. the enrollment wizard's last camera choice): one value per (user, key)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS user_preferences (
        user_id INTEGER NOT NULL REFERENCES users(id),
        key     TEXT NOT NULL,
        value   TEXT,
        updated TEXT NOT NULL,
        PRIMARY KEY (user_id, key)
    )""")


def _v4_drop_unused(conn: sqlite3.Connection) -> None:
    """Remove what nothing uses: the daily_attendance table and sites.timezone (see the module docstring)."""
    conn.execute("DROP TABLE IF EXISTS daily_attendance")
    if "timezone" in _columns(conn, "sites"):
        conn.execute("ALTER TABLE sites DROP COLUMN timezone")


def _v5_shifts_roster(conn: sqlite3.Connection) -> None:
    """Shifts (seeded with config.DEFAULT_SHIFTS) and the roster: which shift a person works on a date, or OFF
    (shift_id NULL). One row per person per date. `start_time`/`end_time` are "HH:MM" (END is an SQL keyword)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS shifts (
        id                   INTEGER PRIMARY KEY,
        name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,
        start_time           TEXT NOT NULL,
        end_time             TEXT NOT NULL,
        crosses_midnight     INTEGER NOT NULL DEFAULT 0,
        grace_min            INTEGER NOT NULL DEFAULT 30,
        in_window_before_min INTEGER NOT NULL DEFAULT 120,
        out_window_after_min INTEGER NOT NULL DEFAULT 240
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS roster (
        id         INTEGER PRIMARY KEY,
        person_id  INTEGER NOT NULL REFERENCES persons(id),
        date       TEXT NOT NULL,
        shift_id   INTEGER REFERENCES shifts(id),
        updated    TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        UNIQUE (person_id, date)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roster_date ON roster(date)")
    if conn.execute("SELECT 1 FROM shifts LIMIT 1").fetchone() is None:
        for s in config.DEFAULT_SHIFTS:
            shift = Shift(s["name"], time.fromisoformat(s["start"]), time.fromisoformat(s["end"]),
                          s["crosses_midnight"])
            conn.execute(f"INSERT INTO shifts ({', '.join(SHIFT_COLUMNS)}) "
                         f"VALUES ({', '.join('?' * len(SHIFT_COLUMNS))})", _shift_values(shift))


VERSIONS = [_v1_users_sessions_sites, _v2_person_sites_original_times,
            _v3_user_preferences, _v4_drop_unused, _v5_shifts_roster]  # PRAGMA user_version = how many have run


def _has_data(conn) -> bool:
    return any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
               for t in ("persons", "cameras", "attendance_events", "audit_log"))


def _backup(conn: sqlite3.Connection, path: Path, version: int) -> Path:
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{path.stem}-v{version}-{datetime.now():%Y%m%d-%H%M%S}.db"
    with closing(sqlite3.connect(dest)) as out:
        conn.backup(out)
    return dest


def _upgrade(conn: sqlite3.Connection, path: Path) -> None:
    """Run the VERSIONS steps this database hasn't had, in one transaction (other processes wait for it)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(VERSIONS):
        return
    # The copy is made before the write lock: SQLite's backup can't read through a connection that holds it.
    backup = _backup(conn, path, version) if _has_data(conn) else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]  # another process may have just done it
        if version >= len(VERSIONS):
            conn.rollback()
            return
        for number, step in enumerate(VERSIONS[version:], start=version + 1):
            step(conn)
            conn.execute(f"PRAGMA user_version = {number}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    print(f"Database {path.name} upgraded from version {version} to {len(VERSIONS)}"
          + (f" (copy of the old one: {backup})" if backup else ""))


def connect(path: Path | None = None, migrate: bool = True) -> sqlite3.Connection:
    """Open the database (default ``config.DB_PATH``), creating or upgrading the tables.

    migrate=False skips the table checks, for callers that already ran them on this file (the API does
    this once at start, not on every request).
    """
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if not migrate:
        return conn
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = _columns(conn, table)
        for col, decl in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    _upgrade(conn, path)
    return conn


def quick_check(path: Path | None = None) -> str | None:
    """PRAGMA quick_check of the database, read-only: None if it is fine (or doesn't exist yet), else the problem.

    The app refuses to start on a damaged database (restore one with scripts/restore.py) rather than write
    attendance into it.
    """
    path = path or config.DB_PATH
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as conn:
            rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"
    result = "; ".join(str(r[0]) for r in rows[:5])
    return None if result == "ok" else result


def require_reason(reason: str | None) -> None:
    if not reason or not reason.strip():
        raise ValueError("a reason is required")


def _audit(conn, user: str, action: str, target: str, details: dict, reason: str) -> None:
    """Write an audit row; call inside the transaction that makes the change."""
    require_reason(reason)
    conn.execute(
        "INSERT INTO audit_log (timestamp, user, action, target, details, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (_ts(datetime.now()), user, action, target, json.dumps(details, default=str), reason.strip()),
    )


# --- People -----------------------------------------------------------------


def sync_persons(conn: sqlite3.Connection, codes: list[str], removed: list[str] = ()) -> None:
    """Make sure everyone in the gallery has a persons row, and deactivate `removed`.

    New codes are added as active (name = code). Existing people keep their
    active flag, so someone an admin deactivated stays inactive after
    re-enrolling. `removed` is who enroll.py's full rebuild dropped from the
    gallery (their known/ folder is gone); people added on the dashboard who
    have no photos yet are never in it, so they are not deactivated.
    """
    with conn:
        for code in codes:
            conn.execute(
                f"INSERT INTO persons (employee_code, name, site_id) VALUES (?, ?, {DEFAULT_SITE}) "
                "ON CONFLICT(employee_code) DO NOTHING",
                (code, code),
            )
        for code in removed:
            conn.execute("UPDATE persons SET active = 0 WHERE employee_code = ?", (code,))


def add_person(conn, code: str, name: str, department: str | None, shift: str | None,
               reason: str, user: str, site_id: int | None = None) -> int:
    """Add a new active person (dashboard Enrollment page, API). `site_id` None = the default site."""
    if not name.strip():
        raise ValueError("name is required")
    if conn.execute("SELECT 1 FROM persons WHERE employee_code = ?", (code,)).fetchone():
        raise ValueError(f"employee code {code!r} already exists")
    row = {"name": name.strip(), "department": (department or "").strip() or None,
           "shift": (shift or "").strip() or None}
    if site_id is not None:
        _site(conn, site_id)
        row["site_id"] = site_id
    with conn:
        cur = conn.execute(
            f"INSERT INTO persons (employee_code, name, department, shift, site_id) "
            f"VALUES (?, ?, ?, ?, COALESCE(?, {DEFAULT_SITE}))",
            (code, row["name"], row["department"], row["shift"], site_id))
        _audit(conn, user, "add_person", f"person:{code}", row, reason)
    return cur.lastrowid


def write_audit(conn, user: str, action: str, target: str, details: dict, reason: str) -> None:
    """An audit row inside the caller's transaction (api/db.py makes its changes and their audit rows together)."""
    _audit(conn, user, action, target, details, reason)


def log_change(conn, user: str, action: str, target: str, details: dict, reason: str) -> None:
    """Audit a settings change (e.g. cameras) that has no row of its own in the database."""
    with conn:
        _audit(conn, user, action, target, details, reason)


def log_face_data_deleted(conn, code: str, details: dict, reason: str, user: str, deactivate: bool) -> None:
    """Audit a face-data deletion (photos and embeddings); attendance history is untouched."""
    with conn:
        _audit(conn, user, "delete_face_data", f"person:{code}", {**details, "deactivated": deactivate}, reason)
        if deactivate:
            conn.execute("UPDATE persons SET active = 0 WHERE employee_code = ?", (code,))


def person_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """employee_code -> id for active people."""
    rows = conn.execute("SELECT id, employee_code FROM persons WHERE active = 1")
    return {r["employee_code"]: r["id"] for r in rows}


def active_person_id(conn: sqlite3.Connection, code: str) -> int | None:
    row = conn.execute("SELECT id FROM persons WHERE employee_code = ? AND active = 1", (code,)).fetchone()
    return row["id"] if row else None


def list_persons(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM persons ORDER BY name").fetchall()


def _site(conn, site_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    if row is None:
        raise ValueError(f"no site with id {site_id}")
    return row


def _person(conn, person_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise ValueError(f"no person with id {person_id}")
    return row


def set_person_active(conn, person_id: int, active: bool, reason: str, user: str) -> None:
    with conn:
        old = _person(conn, person_id)
        _audit(conn, user, "activate_person" if active else "deactivate_person", f"person:{old['employee_code']}",
               {"active": [old["active"], int(active)]}, reason)
        conn.execute("UPDATE persons SET active = ? WHERE id = ?", (int(active), person_id))


def update_person(conn, person_id: int, name: str, department: str | None, shift: str | None,
                  reason: str, user: str, site_id: int | None = None) -> None:
    """Change name, department, shift (and site, if given); audited with the old and new values."""
    if not name.strip():
        raise ValueError("name is required")
    new = {"name": name.strip(), "department": (department or "").strip() or None, "shift": (shift or "").strip() or None}
    if site_id is not None:
        _site(conn, site_id)
        new["site_id"] = site_id
    with conn:
        old = _person(conn, person_id)
        changes = {k: [old[k], v] for k, v in new.items() if old[k] != v}
        if not changes:
            return
        _audit(conn, user, "edit_person", f"person:{old['employee_code']}", changes, reason)
        sets = ", ".join(f"{k} = ?" for k in new)
        conn.execute(f"UPDATE persons SET {sets} WHERE id = ?", (*new.values(), person_id))


# --- Cameras ------------------------------------------------------------------


def upsert_camera(conn: sqlite3.Connection, name: str, source: str | int, direction: str) -> int:
    with conn:
        conn.execute(
            f"INSERT INTO cameras (name, source, direction, site_id) VALUES (?, ?, ?, {DEFAULT_SITE}) "
            "ON CONFLICT(name) DO UPDATE SET source = excluded.source, direction = excluded.direction",
            (name, mask_source(source), direction),  # never store a camera password
        )
    return conn.execute("SELECT id FROM cameras WHERE name = ?", (name,)).fetchone()["id"]


def ensure_cameras(conn: sqlite3.Connection, cameras: list[dict]) -> None:
    """Add configured cameras that aren't in the table yet (keeps sources set by recognize.py)."""
    with conn:
        for cam in cameras:
            conn.execute(
                f"INSERT INTO cameras (name, source, direction, site_id) VALUES (?, ?, ?, {DEFAULT_SITE}) "
                "ON CONFLICT(name) DO NOTHING",
                (cam["name"], mask_source(cam["source"]), cam["direction"]),
            )


def list_cameras(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM cameras ORDER BY name").fetchall()


# --- Events -------------------------------------------------------------------


def add_event(
    conn: sqlite3.Connection,
    person_id: int,
    camera_id: int,
    timestamp: datetime,
    score: float,
    snapshot_path: str | None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO attendance_events (person_id, camera_id, timestamp, match_score, snapshot_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, camera_id, _ts(timestamp), score, snapshot_path),
        )
    return cur.lastrowid


def add_camera_event(conn: sqlite3.Connection, person_id: int, camera_id: int, timestamp: datetime, score: float,
                     snapshot_path: str | None, cooldown_seconds: float) -> tuple[int | None, datetime | None]:
    """A camera's event, unless the person already has one in the same direction (from any camera, not voided)
    within `cooldown_seconds` before it (no reset at midnight): then nothing is written. Returns (new event id or
    None, the latest earlier event's time within that window). Checked and written in one transaction (BEGIN
    IMMEDIATE), so two cameras of the same direction that see the person at the same moment, in different worker
    processes, record it once."""
    since = timestamp - timedelta(seconds=cooldown_seconds)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT MAX(e.timestamp) AS ts FROM attendance_events e JOIN cameras c ON c.id = e.camera_id "
            "WHERE e.person_id = ? AND e.voided = 0 AND c.direction = (SELECT direction FROM cameras WHERE id = ?) "
            "AND e.timestamp >= ? AND e.timestamp <= ?",
            (person_id, camera_id, _ts(since), _ts(timestamp))).fetchone()
        previous = datetime.fromisoformat(row["ts"]) if row["ts"] else None
        if within_cooldown(previous, timestamp, cooldown_seconds):
            conn.rollback()
            return None, previous
        cur = conn.execute(
            "INSERT INTO attendance_events (person_id, camera_id, timestamp, match_score, snapshot_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (person_id, camera_id, _ts(timestamp), score, snapshot_path))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return cur.lastrowid, previous


EVENT_COLUMNS = (
    "SELECT e.id, e.timestamp, e.match_score, e.snapshot_path, e.manual, e.voided, e.original_timestamp, "
    "e.person_id, e.camera_id, c.site_id, "
    "p.employee_code, p.name, c.name AS camera, c.direction FROM attendance_events e "
    "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
)


def _event(conn, event_id: int) -> sqlite3.Row:
    row = conn.execute(EVENT_COLUMNS + "WHERE e.id = ?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"no event with id {event_id}")
    return row


def recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Newest events first, including manual and voided ones."""
    return conn.execute(EVENT_COLUMNS + "ORDER BY e.timestamp DESC, e.id DESC LIMIT ?", (limit,)).fetchall()


def events_on(conn: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    """All of a day's events (including voided) for the corrections page."""
    start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
    return conn.execute(EVENT_COLUMNS + "WHERE e.timestamp >= ? AND e.timestamp < ? ORDER BY e.timestamp",
                        (start, end)).fetchall()


def add_manual_event(conn, person_id: int, camera_id: int, timestamp: datetime, reason: str, user: str) -> int:
    with conn:
        person = _person(conn, person_id)
        cur = conn.execute(
            "INSERT INTO attendance_events (person_id, camera_id, timestamp, match_score, snapshot_path, manual) "
            "VALUES (?, ?, ?, 0, NULL, 1)",
            (person_id, camera_id, _ts(timestamp)),
        )
        camera = conn.execute("SELECT name, direction FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        _audit(conn, user, "add_event", f"event:{cur.lastrowid}",
               {"person": person["employee_code"], "camera": camera["name"], "direction": camera["direction"],
                "timestamp": _ts(timestamp)}, reason)
    return cur.lastrowid


def edit_event_time(conn, event_id: int, timestamp: datetime, reason: str, user: str) -> None:
    """Re-time an event. The time before its first correction stays in original_timestamp (and the audit log)."""
    with conn:
        old = _event(conn, event_id)
        _audit(conn, user, "edit_event", f"event:{event_id}",
               {"person": old["employee_code"], "timestamp": [old["timestamp"], _ts(timestamp)]}, reason)
        conn.execute("UPDATE attendance_events SET original_timestamp = COALESCE(original_timestamp, timestamp), "
                     "timestamp = ? WHERE id = ?", (_ts(timestamp), event_id))


def set_event_voided(conn, event_id: int, voided: bool, reason: str, user: str) -> None:
    with conn:
        old = _event(conn, event_id)
        _audit(conn, user, "void_event" if voided else "restore_event", f"event:{event_id}",
               {"person": old["employee_code"], "timestamp": old["timestamp"], "direction": old["direction"]}, reason)
        conn.execute("UPDATE attendance_events SET voided = ? WHERE id = ?", (int(voided), event_id))


def audit_entries(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def last_event_times(conn: sqlite3.Connection, since: datetime) -> dict[tuple[str, str], datetime]:
    """Each person's latest event per direction at or after `since` (any camera, not voided), to seed a worker's
    cool-down (since = now - the cool-down: there is no reset at midnight)."""
    rows = conn.execute(
        "SELECT p.employee_code, c.direction, MAX(e.timestamp) AS ts FROM attendance_events e "
        "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
        "WHERE e.voided = 0 AND e.timestamp >= ? GROUP BY p.employee_code, c.direction",
        (_ts(since),),
    )
    return {(r["employee_code"], r["direction"]): datetime.fromisoformat(r["ts"]) for r in rows}


def events_for_day(conn: sqlite3.Connection, day: date, person_ids: list[int] | None = None) -> list[Event]:
    """A day's non-voided events (of `person_ids`, or everyone), for the daily summary."""
    return events_between(conn, day, day, person_ids)


def events_between(conn: sqlite3.Connection, first: date, last: date,
                   person_ids: list[int] | None = None) -> list[Event]:
    """The non-voided events of the dates `first` to `last` inclusive (of `person_ids`, or everyone)."""
    start, end = first.isoformat(), (last + timedelta(days=1)).isoformat()
    only = f" AND e.person_id IN ({','.join('?' * len(person_ids))})" if person_ids else ""
    rows = conn.execute(
        "SELECT e.id, p.employee_code, e.timestamp, c.direction FROM attendance_events e "
        "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
        "WHERE e.timestamp >= ? AND e.timestamp < ? AND e.voided = 0" + only + " ORDER BY e.timestamp, e.id",
        (start, end, *(person_ids or ())),
    )
    return [Event(r["employee_code"], datetime.fromisoformat(r["timestamp"]), r["direction"], r["id"]) for r in rows]


def repeat_event_ids(conn: sqlite3.Connection, day: date, person_ids: list[int] | None = None) -> set[int]:
    """The day's repeat events (not the person's first IN or last OUT): kept and listed, but not used for the day."""
    by_person: dict[str, list[Event]] = {}
    for e in events_for_day(conn, day, person_ids):
        by_person.setdefault(e.person, []).append(e)
    return set().union(*(repeat_ids(evs) for evs in by_person.values()))


def day_summaries(conn: sqlite3.Connection, day: date) -> tuple[list[DaySummary], dict[str, int]]:
    """A day's summaries (every active person, plus anyone with events), and employee_code -> person id.

    Read-only: the API's attendance rows, overview and reports, and scripts/today.py all use this, so they agree.
    """
    ids = person_ids(conn)
    events = events_for_day(conn, day)
    # Include people with events even if they were deactivated since.
    for code in {e.person for e in events} - ids.keys():
        ids[code] = conn.execute("SELECT id FROM persons WHERE employee_code = ?", (code,)).fetchone()["id"]
    return summarize_day(events, list(ids)), ids


# --- Shifts and roster (the rules are in app/shifts.py) ------------------------------------------------------------

SHIFT_COLUMNS = ("name", "start_time", "end_time", "crosses_midnight", "grace_min", "in_window_before_min",
                 "out_window_after_min")


def _shift_values(shift: Shift) -> tuple:
    return (shift.name, f"{shift.start:%H:%M}", f"{shift.end:%H:%M}", int(shift.crosses_midnight), shift.grace_min,
            shift.in_window_before_min, shift.out_window_after_min)


def _shift_row(row: sqlite3.Row) -> Shift:
    return Shift(row["name"], time.fromisoformat(row["start_time"]), time.fromisoformat(row["end_time"]),
                 bool(row["crosses_midnight"]), row["grace_min"], row["in_window_before_min"],
                 row["out_window_after_min"], row["id"])


def list_shifts(conn: sqlite3.Connection) -> list[Shift]:
    return [_shift_row(r) for r in conn.execute("SELECT * FROM shifts ORDER BY start_time, name")]


def get_shift(conn: sqlite3.Connection, shift_id: int) -> Shift:
    row = conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        raise ValueError(f"no shift with id {shift_id}")
    return _shift_row(row)


def save_shift(conn: sqlite3.Connection, shift: Shift, reason: str, user: str) -> int:
    """Add a shift (shift.id None) or change one; returns its id. Audited: add_shift, or edit_shift with the old and
    new values. A change applies to every day on that shift, past ones too: the days are computed, not stored."""
    require_reason(reason)
    new = dict(zip(SHIFT_COLUMNS, _shift_values(shift)))
    with conn:
        if conn.execute("SELECT 1 FROM shifts WHERE name = ? AND id IS NOT ?", (shift.name, shift.id)).fetchone():
            raise ValueError(f"a shift named {shift.name!r} already exists")
        if shift.id is None:
            cur = conn.execute(f"INSERT INTO shifts ({', '.join(SHIFT_COLUMNS)}) "
                               f"VALUES ({', '.join('?' * len(SHIFT_COLUMNS))})", tuple(new.values()))
            _audit(conn, user, "add_shift", f"shift:{cur.lastrowid}", new, reason)
            return cur.lastrowid
        old = dict(zip(SHIFT_COLUMNS, _shift_values(get_shift(conn, shift.id))))
        changes = {k: [old[k], v] for k, v in new.items() if old[k] != v}
        if changes:
            _audit(conn, user, "edit_shift", f"shift:{shift.id}", changes, reason)
            conn.execute(f"UPDATE shifts SET {', '.join(f'{k} = ?' for k in SHIFT_COLUMNS)} WHERE id = ?",
                         (*new.values(), shift.id))
    return shift.id


def delete_shift(conn: sqlite3.Connection, shift_id: int, reason: str, user: str) -> None:
    """Remove a shift that no roster row uses (a used one stays: its days are computed from it). Audited."""
    with conn:
        shift = get_shift(conn, shift_id)
        used = conn.execute("SELECT count(*) FROM roster WHERE shift_id = ?", (shift_id,)).fetchone()[0]
        if used:
            raise ValueError(f"shift {shift.name!r} is on {used} roster day(s); change those first")
        _audit(conn, user, "delete_shift", f"shift:{shift_id}", dict(zip(SHIFT_COLUMNS, _shift_values(shift))),
               reason)
        conn.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))


def _roster_day(day) -> str:
    if isinstance(day, datetime) or not isinstance(day, date):
        raise ValueError(f"a roster date must be a date, not {day!r}")
    return day.isoformat()


def _roster_entry(conn, person_id: int, day: str) -> str | None:
    """The person's roster entry on `day`: a shift's name, OFF, or None (no row)."""
    row = conn.execute("SELECT r.shift_id, s.name FROM roster r LEFT JOIN shifts s ON s.id = r.shift_id "
                       "WHERE r.person_id = ? AND r.date = ?", (person_id, day)).fetchone()
    return None if row is None else OFF if row["shift_id"] is None else row["name"]


def set_roster(conn: sqlite3.Connection, entries: list[tuple[int, date, int | None]], reason: str, user: str) -> int:
    """Set roster rows: each (person id, date, shift id or None = OFF) adds or replaces that person's one row for that
    date. All or nothing; one audit row per change (set_roster, the old and new entry: a shift's name, "OFF", or None
    for no row). Returns how many rows changed. Nothing derived is stored: the days around a change are recalculated
    from the raw events the next time they are read."""
    require_reason(reason)
    changed = 0
    with conn:
        for person_id, day, shift_id in entries:
            person, day = _person(conn, person_id), _roster_day(day)
            new = OFF if shift_id is None else get_shift(conn, shift_id).name
            old = _roster_entry(conn, person_id, day)
            if old == new:
                continue
            conn.execute(
                "INSERT INTO roster (person_id, date, shift_id, updated, updated_by) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(person_id, date) DO UPDATE SET shift_id = excluded.shift_id, updated = excluded.updated, "
                "updated_by = excluded.updated_by",
                (person_id, day, shift_id, _ts(datetime.now()), user))
            _audit(conn, user, "set_roster", f"roster:{person['employee_code']}:{day}",
                   {"person": person["employee_code"], "date": day, "shift": [old, new]}, reason)
            changed += 1
    return changed


def clear_roster(conn: sqlite3.Connection, person_id: int, day: date, reason: str, user: str) -> bool:
    """Remove the person's roster row for `day` (the date becomes unscheduled); audited. False if there was none."""
    require_reason(reason)
    with conn:
        person, day = _person(conn, person_id), _roster_day(day)
        old = _roster_entry(conn, person_id, day)
        if old is None:
            return False
        _audit(conn, user, "clear_roster", f"roster:{person['employee_code']}:{day}",
               {"person": person["employee_code"], "date": day, "shift": [old, None]}, reason)
        conn.execute("DELETE FROM roster WHERE person_id = ? AND date = ?", (person_id, day))
    return True


def roster_between(conn: sqlite3.Connection, first: date, last: date) -> dict[tuple[str, date], Shift | None]:
    """(employee_code, date) -> the rostered Shift, or None for OFF, for the dates `first` to `last` inclusive."""
    shifts = {s.id: s for s in list_shifts(conn)}
    rows = conn.execute("SELECT p.employee_code, r.date, r.shift_id FROM roster r JOIN persons p ON p.id = r.person_id "
                        "WHERE r.date >= ? AND r.date <= ?", (first.isoformat(), last.isoformat()))
    return {(r["employee_code"], date.fromisoformat(r["date"])):
            None if r["shift_id"] is None else shifts[r["shift_id"]] for r in rows}


def _roster_inputs(conn, first: date, last: date) -> tuple[list[Event], dict[tuple[str, date], Shift | None]]:
    """The events and roster rows a range needs, ROSTER_PAD_DAYS beyond it (a shift there can take passes in it)."""
    pad = timedelta(days=ROSTER_PAD_DAYS)
    return events_between(conn, first - pad, last + pad), roster_between(conn, first - pad, last + pad)


def roster_days(conn: sqlite3.Connection, first: date, last: date, now: datetime | None = None) -> list[ShiftDay]:
    """The roster days from `first` to `last` (app.shifts.summarize_roster), computed from the raw events: one per
    roster row, plus an "unscheduled" one per person and date with passes but no row. `now` (default: the current
    time) tells which days are still open. Read-only."""
    events, roster = _roster_inputs(conn, first, last)
    return summarize_roster(events, roster, first, last, datetime.now() if now is None else now)


def roster_repeat_event_ids(conn: sqlite3.Connection, first: date, last: date) -> set[int]:
    """Ids of the non-voided events dated `first` to `last` that no roster day uses (app.shifts.roster_repeat_ids)."""
    events, roster = _roster_inputs(conn, first, last)
    repeats = roster_repeat_ids(events, roster)
    return {e.id for e in events if e.id in repeats and first <= e.timestamp.date() <= last}
