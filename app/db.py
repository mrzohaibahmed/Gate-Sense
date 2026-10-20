"""SQLite storage for people, cameras and attendance (data/attendance.db).

Tables follow PLAN.md. Embeddings stay in data/gallery.npz for now, so there is
no face_embeddings table yet. ``cameras.source`` replaces PLAN.md's
``rtsp_url`` because a source can also be a webcam index or a video file.

Timestamps are local time stored as ISO text ("YYYY-MM-DD HH:MM:SS"). WAL mode
lets one recognize.py per camera write at the same time.
"""

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
    snapshot_path TEXT
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
"""


def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(sep=" ", timespec="seconds")


def connect(path: Path = config.DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the file and tables if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def sync_persons(conn: sqlite3.Connection, codes: list[str]) -> None:
    """Make exactly these employee codes active; new ones get their code as name."""
    with conn:
        conn.execute("UPDATE persons SET active = 0")
        for code in codes:
            conn.execute(
                "INSERT INTO persons (employee_code, name) VALUES (?, ?) "
                "ON CONFLICT(employee_code) DO UPDATE SET active = 1",
                (code, code),
            )


def person_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """employee_code -> id for active people."""
    rows = conn.execute("SELECT id, employee_code FROM persons WHERE active = 1")
    return {r["employee_code"]: r["id"] for r in rows}


def upsert_camera(conn: sqlite3.Connection, name: str, source: str | int, direction: str) -> int:
    with conn:
        conn.execute(
            "INSERT INTO cameras (name, source, direction) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET source = excluded.source, direction = excluded.direction",
            (name, str(source), direction),
        )
    return conn.execute("SELECT id FROM cameras WHERE name = ?", (name,)).fetchone()["id"]


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


def last_event_times(conn: sqlite3.Connection, camera_name: str) -> dict[tuple[str, str], datetime]:
    """Latest event per person on this camera, to seed the cool-down after a restart."""
    rows = conn.execute(
        "SELECT p.employee_code, MAX(e.timestamp) AS ts FROM attendance_events e "
        "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
        "WHERE c.name = ? GROUP BY p.employee_code",
        (camera_name,),
    )
    return {(r["employee_code"], camera_name): datetime.fromisoformat(r["ts"]) for r in rows}


def events_for_day(conn: sqlite3.Connection, day: date) -> list[Event]:
    start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT p.employee_code, e.timestamp, c.direction FROM attendance_events e "
        "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
        "WHERE e.timestamp >= ? AND e.timestamp < ? ORDER BY e.timestamp",
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
        for s in summaries:
            conn.execute(
                "INSERT INTO daily_attendance (person_id, date, first_in, last_out, worked_hours, status) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(person_id, date) DO UPDATE SET "
                "first_in = excluded.first_in, last_out = excluded.last_out, "
                "worked_hours = excluded.worked_hours, status = excluded.status",
                (ids[s.person], day.isoformat(), _ts(s.first_in), _ts(s.last_out), s.worked_hours, s.status),
            )
    return summaries
