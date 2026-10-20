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
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from app import config
from app.attendance import DaySummary, Event, summarize_day

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
CREATE TABLE IF NOT EXISTS daily_attendance (
    person_id    INTEGER NOT NULL REFERENCES persons(id),
    date         TEXT NOT NULL,
    first_in     TEXT,
    last_out     TEXT,
    worked_hours REAL,
    status       TEXT NOT NULL,
    PRIMARY KEY (person_id, date)
);
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


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database (default ``config.DB_PATH``), creating or upgrading the tables."""
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


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
                "INSERT INTO persons (employee_code, name) VALUES (?, ?) ON CONFLICT(employee_code) DO NOTHING",
                (code, code),
            )
        for code in removed:
            conn.execute("UPDATE persons SET active = 0 WHERE employee_code = ?", (code,))


def add_person(conn, code: str, name: str, department: str | None, shift: str | None,
               reason: str, user: str) -> int:
    """Add a new active person (from the dashboard's Enrollment page)."""
    if not name.strip():
        raise ValueError("name is required")
    if conn.execute("SELECT 1 FROM persons WHERE employee_code = ?", (code,)).fetchone():
        raise ValueError(f"employee code {code!r} already exists")
    row = {"name": name.strip(), "department": (department or "").strip() or None,
           "shift": (shift or "").strip() or None}
    with conn:
        cur = conn.execute("INSERT INTO persons (employee_code, name, department, shift) VALUES (?, ?, ?, ?)",
                           (code, row["name"], row["department"], row["shift"]))
        _audit(conn, user, "add_person", f"person:{code}", row, reason)
    return cur.lastrowid


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
                  reason: str, user: str) -> None:
    if not name.strip():
        raise ValueError("name is required")
    new = {"name": name.strip(), "department": (department or "").strip() or None, "shift": (shift or "").strip() or None}
    with conn:
        old = _person(conn, person_id)
        changes = {k: [old[k], v] for k, v in new.items() if old[k] != v}
        if not changes:
            return
        _audit(conn, user, "edit_person", f"person:{old['employee_code']}", changes, reason)
        conn.execute("UPDATE persons SET name = ?, department = ?, shift = ? WHERE id = ?",
                     (new["name"], new["department"], new["shift"], person_id))


# --- Cameras ------------------------------------------------------------------


def upsert_camera(conn: sqlite3.Connection, name: str, source: str | int, direction: str) -> int:
    with conn:
        conn.execute(
            "INSERT INTO cameras (name, source, direction) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET source = excluded.source, direction = excluded.direction",
            (name, str(source), direction),
        )
    return conn.execute("SELECT id FROM cameras WHERE name = ?", (name,)).fetchone()["id"]


def ensure_cameras(conn: sqlite3.Connection, cameras: list[dict]) -> None:
    """Add configured cameras that aren't in the table yet (keeps sources set by recognize.py)."""
    with conn:
        for cam in cameras:
            conn.execute(
                "INSERT INTO cameras (name, source, direction) VALUES (?, ?, ?) ON CONFLICT(name) DO NOTHING",
                (cam["name"], str(cam["source"]), cam["direction"]),
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


EVENT_COLUMNS = (
    "SELECT e.id, e.timestamp, e.match_score, e.snapshot_path, e.manual, e.voided, "
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
    with conn:
        old = _event(conn, event_id)
        _audit(conn, user, "edit_event", f"event:{event_id}",
               {"person": old["employee_code"], "timestamp": [old["timestamp"], _ts(timestamp)]}, reason)
        conn.execute("UPDATE attendance_events SET timestamp = ? WHERE id = ?", (_ts(timestamp), event_id))


def set_event_voided(conn, event_id: int, voided: bool, reason: str, user: str) -> None:
    with conn:
        old = _event(conn, event_id)
        _audit(conn, user, "void_event" if voided else "restore_event", f"event:{event_id}",
               {"person": old["employee_code"], "timestamp": old["timestamp"], "direction": old["direction"]}, reason)
        conn.execute("UPDATE attendance_events SET voided = ? WHERE id = ?", (int(voided), event_id))


def audit_entries(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def last_event_times(conn: sqlite3.Connection, camera_name: str) -> dict[tuple[str, str], datetime]:
    """Latest event per person on this camera, to seed the cool-down after a restart."""
    rows = conn.execute(
        "SELECT p.employee_code, MAX(e.timestamp) AS ts FROM attendance_events e "
        "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
        "WHERE c.name = ? AND e.voided = 0 GROUP BY p.employee_code",
        (camera_name,),
    )
    return {(r["employee_code"], camera_name): datetime.fromisoformat(r["ts"]) for r in rows}


def events_for_day(conn: sqlite3.Connection, day: date) -> list[Event]:
    """A day's non-voided events, for the daily summary."""
    start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT p.employee_code, e.timestamp, c.direction FROM attendance_events e "
        "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
        "WHERE e.timestamp >= ? AND e.timestamp < ? AND e.voided = 0 ORDER BY e.timestamp",
        (start, end),
    )
    return [Event(r["employee_code"], datetime.fromisoformat(r["timestamp"]), r["direction"]) for r in rows]


def refresh_daily(conn: sqlite3.Connection, day: date) -> list[DaySummary]:
    """Recompute daily_attendance for `day` from the raw events and return it."""
    ids = person_ids(conn)
    events = events_for_day(conn, day)
    # Include people with events even if they were deactivated since.
    for code in {e.person for e in events} - ids.keys():
        ids[code] = conn.execute("SELECT id FROM persons WHERE employee_code = ?", (code,)).fetchone()["id"]
    summaries = summarize_day(events, list(ids))
    with conn:
        conn.execute("DELETE FROM daily_attendance WHERE date = ?", (day.isoformat(),))
        for s in summaries:
            conn.execute(
                "INSERT INTO daily_attendance (person_id, date, first_in, last_out, worked_hours, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ids[s.person], day.isoformat(), _ts(s.first_in), _ts(s.last_out), s.worked_hours, s.status),
            )
    return summaries
