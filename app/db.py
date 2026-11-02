"""Storage for people, cameras and attendance, in SQL Server (app/mssql.py).

Tables follow PLAN.md. Embeddings stay in data/gallery.npz for now, so there is
no face_embeddings table yet. ``cameras.source`` replaces PLAN.md's
``rtsp_url`` because a source can also be a webcam index or a video file.

Manual corrections never delete anything: an admin can add a manual event,
change an event's time, or void (and restore) an event. Every change, and
every change to a person, writes an ``audit_log`` row with a required reason
in the same transaction. Voided events are ignored by the daily summary.
Manual events have ``manual = 1`` and ``match_score = 0``.

Timestamps are local time, passed and read back as ISO text ("YYYY-MM-DD HH:MM:SS").
"user" and "key" are quoted: they are reserved words. Roster days are computed from the
raw events by app/shifts.py, so nothing derived is stored; a shift's times live in
shift_versions (effective_from: NULL = from the start), so a change applies from a date
and never rewrites the days before it. app_settings holds settings kept in the database,
read on every request. unknown_visits are the faces a camera saw that matched nobody
enrolled (app/visitors.py), never attendance.
"""

import json
from datetime import date, datetime, time, timedelta

from app import config, dbconn, mssql
from app.attendance import Event, within_cooldown
from app.camera import mask_source
from app.mssql import Connection, Row
from app.shifts import (
    OFF, ROSTER_PAD_DAYS, Shift, ShiftDay, calendar_repeat_ids, roster_repeat_ids, summarize_calendar, summarize_roster,
    version_on,
)

def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(sep=" ", timespec="seconds")


DEFAULT_SITE = "(SELECT MIN(id) FROM sites)"  # SQL for the default site's id


def connect(migrate: bool = True) -> Connection:
    """Open the database (GATESENSE_DB_URL), creating or upgrading the tables (app.mssql.connect).

    migrate=False skips the table checks, for callers that already ran them (the API does this once at start,
    not on every request).
    """
    return mssql.connect(dbconn.server_url(), migrate)


def schema_version(conn) -> int:
    """The schema version (0 = no tables yet)."""
    return mssql.schema_version(conn) or 0


def _merge(table: str, columns: list[str], keys: list[str], update: list[str], computed: dict | None = None) -> str:
    """An insert-or-update on `keys`, setting `update` on an existing row (leaving it as it is if `update` is
    empty): the parameters are `columns`' values, in order; `computed` = more inserted columns' SQL expressions."""
    computed = computed or {}
    q = '"{}"'.format
    source = ", ".join([f"? AS {q(c)}" for c in columns] + [f"{sql} AS {q(c)}" for c, sql in computed.items()])
    inserted = [*columns, *computed]
    sql = (f"MERGE INTO {table} WITH (HOLDLOCK) AS t USING (SELECT {source}) AS s ON "
           + " AND ".join(f"t.{q(k)} = s.{q(k)}" for k in keys))
    if update:
        sql += " WHEN MATCHED THEN UPDATE SET " + ", ".join(f"{q(c)} = s.{q(c)}" for c in update)
    return (sql + f" WHEN NOT MATCHED THEN INSERT ({', '.join(map(q, inserted))}) "
            f"VALUES ({', '.join('s.' + q(c) for c in inserted)});")


def require_reason(reason: str | None) -> None:
    if not reason or not reason.strip():
        raise ValueError("a reason is required")


def _audit(conn, user: str, action: str, target: str, details: dict, reason: str) -> None:
    """Write an audit row; call inside the transaction that makes the change."""
    require_reason(reason)
    conn.execute(
        'INSERT INTO audit_log (timestamp, "user", action, target, details, reason) VALUES (?, ?, ?, ?, ?, ?)',
        (_ts(datetime.now()), user, action, target, json.dumps(details, default=str), reason.strip()),
    )


# --- People -----------------------------------------------------------------


def sync_persons(conn: Connection, codes: list[str], removed: list[str] = ()) -> None:
    """Make sure everyone in the gallery has a persons row, and deactivate `removed`.

    New codes are added as active (name = code). Existing people keep their
    active flag, so someone an admin deactivated stays inactive after
    re-enrolling. `removed` is who enroll.py's full rebuild dropped from the
    gallery (their known/ folder is gone); people added on the dashboard who
    have no photos yet are never in it, so they are not deactivated.
    """
    sql = _merge("persons", ["employee_code", "name"], ["employee_code"], [], {"site_id": DEFAULT_SITE})
    with conn:
        for code in codes:
            conn.execute(sql, (code, code))
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
            "INSERT INTO persons (employee_code, name, department, shift, site_id) "
            f"SELECT ?, ?, ?, ?, COALESCE(?, {DEFAULT_SITE})",  # no subquery allowed in VALUES
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


def person_ids(conn: Connection) -> dict[str, int]:
    """employee_code -> id for active people."""
    rows = conn.execute("SELECT id, employee_code FROM persons WHERE active = 1")
    return {r["employee_code"]: r["id"] for r in rows}


def active_person_id(conn: Connection, code: str) -> int | None:
    row = conn.execute("SELECT id FROM persons WHERE employee_code = ? AND active = 1", (code,)).fetchone()
    return row["id"] if row else None


def list_persons(conn: Connection) -> list[Row]:
    return conn.execute("SELECT * FROM persons ORDER BY name").fetchall()


def _site(conn, site_id: int) -> Row:
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    if row is None:
        raise ValueError(f"no site with id {site_id}")
    return row


def _person(conn, person_id: int) -> Row:
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


def upsert_camera(conn: Connection, name: str, source: str | int, direction: str) -> int:
    sql = _merge("cameras", ["name", "source", "direction"], ["name"], ["source", "direction"],
                 {"site_id": DEFAULT_SITE})
    with conn:
        conn.execute(sql, (name, mask_source(source), direction))  # never store a camera password
    return conn.execute("SELECT id FROM cameras WHERE name = ?", (name,)).fetchone()["id"]


def ensure_cameras(conn: Connection, cameras: list[dict]) -> None:
    """Add configured cameras that aren't in the table yet (keeps sources set by recognize.py)."""
    sql = _merge("cameras", ["name", "source", "direction"], ["name"], [], {"site_id": DEFAULT_SITE})
    with conn:
        for cam in cameras:
            conn.execute(sql, (cam["name"], mask_source(cam["source"]), cam["direction"]))


def list_cameras(conn: Connection) -> list[Row]:
    return conn.execute("SELECT * FROM cameras ORDER BY name").fetchall()


# --- Events -------------------------------------------------------------------


def add_event(
    conn: Connection,
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


def add_camera_event(conn: Connection, person_id: int, camera_id: int, timestamp: datetime, score: float,
                     snapshot_path: str | None, cooldown_seconds: float,
                     retry: bool = False) -> tuple[int | None, datetime | None]:
    """A camera's event, unless the person already has one in the same direction (from any camera, not voided)
    within `cooldown_seconds` before it (no reset at midnight): then nothing is written. Returns (new event id or
    None, the latest earlier event's time within that window). Checked and written in one transaction (BEGIN
    IMMEDIATE), so two cameras of the same direction that see the person at the same moment, in different worker
    processes, record it once.

    `retry`: an earlier try of this same event failed, maybe after the database had committed it (the
    connection lost during COMMIT). If the identical camera event (person, camera, time, score, snapshot) is there,
    that is it: its id is returned, with no earlier time, and nothing is written twice."""
    since = timestamp - timedelta(seconds=cooldown_seconds)
    conn.begin()
    try:
        if retry:
            same = conn.execute(
                "SELECT id FROM attendance_events WHERE person_id = ? AND camera_id = ? AND timestamp = ? "
                "AND match_score = ? AND manual = 0 AND "
                + ("snapshot_path IS NULL" if snapshot_path is None else "snapshot_path = ?"),
                (person_id, camera_id, _ts(timestamp), score, *(() if snapshot_path is None else (snapshot_path,)))
            ).fetchone()
            if same:
                conn.rollback()
                return same["id"], None
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


def _event(conn, event_id: int) -> Row:
    row = conn.execute(EVENT_COLUMNS + "WHERE e.id = ?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"no event with id {event_id}")
    return row


def recent_events(conn: Connection, limit: int = 50) -> list[Row]:
    """Newest events first, including manual and voided ones."""
    return conn.execute(EVENT_COLUMNS + "ORDER BY e.timestamp DESC, e.id DESC" + dbconn.LIMIT,
                        (limit,)).fetchall()


def events_on(conn: Connection, day: date) -> list[Row]:
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


def audit_entries(conn: Connection, limit: int = 500) -> list[Row]:
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC" + dbconn.LIMIT, (limit,)).fetchall()


def last_event_times(conn: Connection, since: datetime) -> dict[tuple[str, str], datetime]:
    """Each person's latest event per direction at or after `since` (any camera, not voided), to seed a worker's
    cool-down (since = now - the cool-down: there is no reset at midnight)."""
    rows = conn.execute(
        "SELECT p.employee_code, c.direction, MAX(e.timestamp) AS ts FROM attendance_events e "
        "JOIN persons p ON p.id = e.person_id JOIN cameras c ON c.id = e.camera_id "
        "WHERE e.voided = 0 AND e.timestamp >= ? GROUP BY p.employee_code, c.direction",
        (_ts(since),),
    )
    return {(r["employee_code"], r["direction"]): datetime.fromisoformat(r["ts"]) for r in rows}


def events_between(conn: Connection, first: date, last: date,
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


# --- Shifts and roster (the rules are in app/shifts.py) ------------------------------------------------------------
#
# A shift is a name (shifts) plus versions (shift_versions): the times, grace and windows in force from a date. The
# first version is from the start (effective_from NULL); changing the times adds a version from the date the user
# picks, so the days before it keep the times they had.

RULE_COLUMNS = ("start_time", "end_time", "crosses_midnight", "grace_min", "in_window_before_min",
                "out_window_after_min")


def _rule_values(shift: Shift) -> tuple:
    return (f"{shift.start:%H:%M}", f"{shift.end:%H:%M}", int(shift.crosses_midnight), shift.grace_min,
            shift.in_window_before_min, shift.out_window_after_min)


def _rule_details(shift: Shift) -> dict:
    return dict(zip(RULE_COLUMNS, _rule_values(shift)))


def _version_shift(row: Row, name: str) -> Shift:
    return Shift(name, time.fromisoformat(row["start_time"]), time.fromisoformat(row["end_time"]),
                 bool(row["crosses_midnight"]), row["grace_min"], row["in_window_before_min"],
                 row["out_window_after_min"], row["shift_id"],
                 date.fromisoformat(row["effective_from"]) if row["effective_from"] else None)


FROM_START_FIRST = "CASE WHEN effective_from IS NULL THEN 0 ELSE 1 END"  # the version from the start sorts first


def shift_versions(conn: Connection, shift_id: int | None = None) -> dict[int, list[Shift]]:
    """shift id -> its versions, oldest first (the first one from the start), for one shift or all of them."""
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM shifts")}
    only = " WHERE shift_id = ?" if shift_id is not None else ""
    rows = conn.execute(f"SELECT * FROM shift_versions{only} ORDER BY shift_id, {FROM_START_FIRST}, effective_from",
                        () if shift_id is None else (shift_id,))
    out: dict[int, list[Shift]] = {}
    for r in rows:
        out.setdefault(r["shift_id"], []).append(_version_shift(r, names[r["shift_id"]]))
    return out


def shift_version_rows(conn: Connection, shift_id: int) -> list[Row]:
    """One shift's version rows (id, effective_from, times, created, created_by), oldest first."""
    return conn.execute(f"SELECT * FROM shift_versions WHERE shift_id = ? ORDER BY {FROM_START_FIRST}, "
                        "effective_from", (shift_id,)).fetchall()


def list_shifts(conn: Connection, on: date | None = None) -> list[Shift]:
    """Every shift as in force on `on` (default today), by start time then name."""
    day = on or date.today()
    return sorted((version_on(vs, day) for vs in shift_versions(conn).values()),
                  key=lambda s: (s.start, s.name.lower()))


def get_shift(conn: Connection, shift_id: int, on: date | None = None) -> Shift:
    """The shift as in force on `on` (default today); ValueError if there is no such shift."""
    versions = shift_versions(conn, shift_id).get(shift_id)
    if not versions:
        raise ValueError(f"no shift with id {shift_id}")
    return version_on(versions, on or date.today())


def _unique_name(conn, name: str, shift_id: int | None) -> None:
    other = () if shift_id is None else (shift_id,)
    if conn.execute("SELECT 1 FROM shifts WHERE name = ?" + (" AND id <> ?" if other else ""),
                    (name, *other)).fetchone():
        raise ValueError(f"a shift named {name!r} already exists")


def add_shift(conn: Connection, shift: Shift, reason: str, user: str) -> int:
    """A new shift with its first version (from the start); returns its id. Audited (add_shift)."""
    require_reason(reason)
    with conn:
        _unique_name(conn, shift.name, None)
        shift_id = conn.execute("INSERT INTO shifts (name) VALUES (?)", (shift.name,)).lastrowid
        _insert_version(conn, shift_id, None, shift, user)
        _audit(conn, user, "add_shift", f"shift:{shift_id}", {"name": shift.name, **_rule_details(shift)}, reason)
    return shift_id


def _insert_version(conn, shift_id: int, effective_from: date | None, shift: Shift, user: str) -> None:
    conn.execute(f"INSERT INTO shift_versions (shift_id, effective_from, {', '.join(RULE_COLUMNS)}, created, "
                 f"created_by) VALUES (?, ?, {', '.join('?' * len(RULE_COLUMNS))}, ?, ?)",
                 (shift_id, effective_from.isoformat() if effective_from else None, *_rule_values(shift),
                  _ts(datetime.now()), user))


def change_shift(conn: Connection, shift_id: int, rules: Shift, effective_from: date, reason: str,
                 user: str) -> bool:
    """New times (grace, windows) for a shift from `effective_from` on: a new version, or the one that already starts
    that day is replaced. The days before keep their times. Audited (edit_shift: the date and the old and new values
    in force on it). False if nothing changes. `rules.name` is ignored (rename_shift changes the name)."""
    require_reason(reason)
    if isinstance(effective_from, datetime) or not isinstance(effective_from, date):
        raise ValueError("the change needs the date it applies from")
    with conn:
        old = get_shift(conn, shift_id, effective_from)
        if old.rules() == rules.rules():
            return False
        same_day = conn.execute("SELECT id FROM shift_versions WHERE shift_id = ? AND effective_from = ?",
                                (shift_id, effective_from.isoformat())).fetchone()
        if same_day:
            conn.execute(f"UPDATE shift_versions SET {', '.join(f'{c} = ?' for c in RULE_COLUMNS)}, created = ?, "
                         "created_by = ? WHERE id = ?",
                         (*_rule_values(rules), _ts(datetime.now()), user, same_day["id"]))
        else:
            _insert_version(conn, shift_id, effective_from, rules, user)
        before, after = _rule_details(old), _rule_details(rules)
        _audit(conn, user, "edit_shift", f"shift:{shift_id}",
               {"name": old.name, "effective_from": effective_from.isoformat(),
                **{k: [before[k], v] for k, v in after.items() if before[k] != v}}, reason)
    return True


def rename_shift(conn: Connection, shift_id: int, name: str, reason: str, user: str) -> bool:
    """Change a shift's name (everywhere, past days too: it is only a label). Audited (edit_shift). False if the
    same."""
    require_reason(reason)
    renamed = Shift(name, time(8), time(16), False).name  # the same checks as a new shift's name
    with conn:
        old = get_shift(conn, shift_id).name
        if old == renamed:
            return False
        _unique_name(conn, renamed, shift_id)
        conn.execute("UPDATE shifts SET name = ? WHERE id = ?", (renamed, shift_id))
        _audit(conn, user, "edit_shift", f"shift:{shift_id}", {"name": [old, renamed]}, reason)
    return True


def delete_shift_version(conn: Connection, shift_id: int, version_id: int, reason: str, user: str) -> None:
    """Withdraw a planned change: only a version that isn't in force yet (it starts after today) can go. Audited."""
    require_reason(reason)
    with conn:
        row = conn.execute("SELECT * FROM shift_versions WHERE id = ? AND shift_id = ?",
                           (version_id, shift_id)).fetchone()
        if row is None:
            raise LookupError(f"shift {shift_id} has no version {version_id}")
        if not row["effective_from"] or date.fromisoformat(row["effective_from"]) <= date.today():
            raise ValueError("only a change that hasn't started yet can be withdrawn; to change the times again, "
                             "add a new change from a date")
        name = get_shift(conn, shift_id).name
        _audit(conn, user, "delete_shift_version", f"shift:{shift_id}",
               {"name": name, "effective_from": row["effective_from"], **{c: row[c] for c in RULE_COLUMNS}}, reason)
        conn.execute("DELETE FROM shift_versions WHERE id = ?", (version_id,))


def shift_roster_days(conn: Connection) -> dict[int, int]:
    """shift id -> how many roster rows use it."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT shift_id, count(*) FROM roster WHERE shift_id IS NOT NULL GROUP BY shift_id")}


def delete_shift(conn: Connection, shift_id: int, reason: str, user: str) -> None:
    """Remove a shift that no roster row uses (a used one stays: its days are computed from it). Audited."""
    require_reason(reason)
    with conn:
        shift = get_shift(conn, shift_id)
        used = shift_roster_days(conn).get(shift_id, 0)
        if used:
            raise ValueError(f"shift {shift.name!r} is on {used} roster day(s); change those first")
        _audit(conn, user, "delete_shift", f"shift:{shift_id}", {"name": shift.name, **_rule_details(shift)}, reason)
        conn.execute("DELETE FROM shift_versions WHERE shift_id = ?", (shift_id,))
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


def write_roster(conn: Connection, entries: list[tuple[int, date, int | None]], reason: str,
                 user: str) -> int:
    """set_roster's work inside the caller's transaction (people import style); returns how many rows changed.
    An entry's shift id None = OFF."""
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM shifts")}
    codes: dict[int, str] = {}
    changed = 0
    upsert = _merge("roster", ["person_id", "date", "shift_id", "updated", "updated_by"], ["person_id", "date"],
                    ["shift_id", "updated", "updated_by"])
    for person_id, day, shift_id in entries:
        if person_id not in codes:
            codes[person_id] = _person(conn, person_id)["employee_code"]
        day = _roster_day(day)
        if shift_id is not None and shift_id not in names:
            raise ValueError(f"no shift with id {shift_id}")
        new = OFF if shift_id is None else names[shift_id]
        old = _roster_entry(conn, person_id, day)
        if old == new:
            continue
        conn.execute(upsert, (person_id, day, shift_id, _ts(datetime.now()), user))
        _audit(conn, user, "set_roster", f"roster:{codes[person_id]}:{day}",
               {"person": codes[person_id], "date": day, "shift": [old, new]}, reason)
        changed += 1
    return changed


def set_roster(conn: Connection, entries: list[tuple[int, date, int | None]], reason: str, user: str) -> int:
    """Set roster rows: each (person id, date, shift id or None = OFF) adds or replaces that person's one row for that
    date. All or nothing; one audit row per change (set_roster, the old and new entry: a shift's name, "OFF", or None
    for no row). Returns how many rows changed. Nothing derived is stored: the days around a change are recalculated
    from the raw events the next time they are read."""
    require_reason(reason)
    with conn:
        return write_roster(conn, entries, reason, user)


def clear_roster_rows(conn: Connection, entries: list[tuple[int, date]], reason: str, user: str) -> int:
    """Remove roster rows (each date becomes unscheduled for that person) inside the caller's transaction; audited
    (clear_roster). Returns how many there were."""
    removed = 0
    for person_id, day in entries:
        person, day = _person(conn, person_id), _roster_day(day)
        old = _roster_entry(conn, person_id, day)
        if old is None:
            continue
        _audit(conn, user, "clear_roster", f"roster:{person['employee_code']}:{day}",
               {"person": person["employee_code"], "date": day, "shift": [old, None]}, reason)
        conn.execute("DELETE FROM roster WHERE person_id = ? AND date = ?", (person_id, day))
        removed += 1
    return removed


def clear_roster(conn: Connection, person_id: int, day: date, reason: str, user: str) -> bool:
    """Remove the person's roster row for `day` (the date becomes unscheduled); audited. False if there was none."""
    require_reason(reason)
    with conn:
        return clear_roster_rows(conn, [(person_id, day)], reason, user) == 1


def roster_rows(conn: Connection, first: date, last: date,
                person_ids: list[int] | None = None) -> list[Row]:
    """The roster rows (person_id, employee_code, date, shift_id) of the dates `first` to `last`."""
    only = f" AND r.person_id IN ({','.join('?' * len(person_ids))})" if person_ids else ""
    return conn.execute("SELECT r.person_id, p.employee_code, r.date, r.shift_id FROM roster r "
                        "JOIN persons p ON p.id = r.person_id WHERE r.date >= ? AND r.date <= ?" + only
                        + " ORDER BY r.date, r.person_id", (first.isoformat(), last.isoformat(),
                                                            *(person_ids or ()))).fetchall()


def roster_between(conn: Connection, first: date, last: date) -> dict[tuple[str, date], Shift | None]:
    """(employee_code, date) -> the rostered shift as in force on that date, or None for OFF, for the dates `first`
    to `last` inclusive."""
    versions = shift_versions(conn)
    out = {}
    for r in roster_rows(conn, first, last):
        day = date.fromisoformat(r["date"])
        out[r["employee_code"], day] = None if r["shift_id"] is None else version_on(versions[r["shift_id"]], day)
    return out


def clock() -> datetime:
    """Now, for the days still running (open, "not in yet"); tests replace it."""
    return datetime.now()


def _roster_inputs(conn, first: date, last: date) -> tuple[list[Event], dict[tuple[str, date], Shift | None]]:
    """The events and roster rows a range needs, ROSTER_PAD_DAYS beyond it (a shift there can take passes in it)."""
    pad = timedelta(days=ROSTER_PAD_DAYS)
    return events_between(conn, first - pad, last + pad), roster_between(conn, first - pad, last + pad)


def roster_days(conn: Connection, first: date, last: date, now: datetime | None = None) -> list[ShiftDay]:
    """The roster days from `first` to `last` (app.shifts.summarize_roster), computed from the raw events: one per
    roster row, plus an "unscheduled" one per person and date with passes but no row. `now` (default: clock(), the
    current time) tells which days are still open. Read-only: the API's attendance rows, overview, reports and
    scripts/today.py all use this, so they agree."""
    events, roster = _roster_inputs(conn, first, last)
    return summarize_roster(events, roster, first, last, clock() if now is None else now)


def roster_repeat_event_ids(conn: Connection, first: date, last: date,
                            now: datetime | None = None) -> set[int]:
    """Ids of the non-voided events dated `first` to `last` that no roster day uses (app.shifts.roster_repeat_ids)."""
    events, roster = _roster_inputs(conn, first, last)
    repeats = roster_repeat_ids(events, roster, clock() if now is None else now)
    return {e.id for e in events if e.id in repeats and first <= e.timestamp.date() <= last}


# --- Shifts on or off (app_settings "shifts") ------------------------------------------------------------------------
#
# On (the default): a day is a roster day (summarize_roster). Off: the calendar day as before the shift module
# (summarize_calendar). Switching changes nothing stored (shifts, roster rows and raw events stay as they are), so
# switching back shows the shift results again, recalculated from the raw events.


def _switch(conn, key: str, default: bool) -> dict:
    """An on/off setting in app_settings: {"enabled", "updated", "updated_by"} (no row: `default`, None, None)."""
    row = conn.execute('SELECT value, updated, updated_by FROM app_settings WHERE "key" = ?', (key,)).fetchone()
    if row is None:
        return {"enabled": default, "updated": None, "updated_by": None}
    return {"enabled": row["value"] == "on", "updated": row["updated"], "updated_by": row["updated_by"]}


def _set_switch(conn, key: str, enabled: bool, user: str) -> None:
    """Write an on/off setting inside the caller's transaction."""
    values = (key, "on" if enabled else "off", _ts(datetime.now()), user)
    conn.execute(_merge("app_settings", ["key", "value", "updated", "updated_by"], ["key"],
                        ["value", "updated", "updated_by"]), values)


def shift_mode(conn: Connection) -> dict:
    """{"enabled": bool, "updated": text or None, "updated_by": text or None}, read from the database each time."""
    return _switch(conn, "shifts", config.SHIFTS_ENABLED_DEFAULT)


def shifts_enabled(conn: Connection) -> bool:
    return shift_mode(conn)["enabled"]


def set_shifts_enabled(conn: Connection, enabled: bool, reason: str, user: str) -> bool:
    """Switch shifts on or off; audited (enable_shifts / disable_shifts). False if it already was."""
    require_reason(reason)
    with conn:
        if shifts_enabled(conn) == enabled:
            return False
        _set_switch(conn, "shifts", enabled, user)
        _audit(conn, user, "enable_shifts" if enabled else "disable_shifts", "settings:shifts",
               {"shifts": ["off" if enabled else "on", "on" if enabled else "off"]}, reason)
    return True


def attendance_days(conn: Connection, first: date, last: date, now: datetime | None = None) -> list[ShiftDay]:
    """The days every screen, report and scripts/today.py show: roster days with shifts on, calendar days with them
    off (shift_mode). Read-only."""
    if shifts_enabled(conn):
        return roster_days(conn, first, last, now)
    return summarize_calendar(events_between(conn, first, last), first, last, clock() if now is None else now)


def attendance_repeat_event_ids(conn: Connection, first: date, last: date) -> set[int]:
    """The repeat events dated `first` to `last` under the rule in use (shifts on: roster days; off: calendar days)."""
    if shifts_enabled(conn):
        return roster_repeat_event_ids(conn, first, last)
    return calendar_repeat_ids(events_between(conn, first, last))


# --- Unknown visitors (app/visitors.py; the "unknown_visitors" setting in app_settings) ------------------------------
#
# The camera worker adds a visit while the person is in view, writes its last-seen time when the track ends and
# withdraws its own record if the track is recognised after all; people review visits on the dashboard (audited); the
# nightly job deletes them after UNKNOWN_RETENTION_DAYS. Attendance never changes here.

UNKNOWN_LOG = "unknown_visitors"
VISIT_STATUSES = ("new", "dismissed", "identified")
UNKNOWN_FROM = ("FROM unknown_visits v JOIN cameras c ON c.id = v.camera_id "
                "LEFT JOIN persons cp ON cp.employee_code = v.closest_code LEFT JOIN persons p ON p.id = v.person_id ")
UNKNOWN_COLUMNS = (
    "SELECT v.id, v.camera_id, c.name AS camera, c.direction, c.site_id, v.first_seen, v.last_seen, v.tracks, "
    "v.best_score, v.closest_code, cp.name AS closest_name, v.liveness, v.face_px, v.snapshot_path, v.status, "
    "v.person_id, p.employee_code AS person_code, p.name AS person_name, v.note, v.reviewed, v.reviewed_by "
    + UNKNOWN_FROM)


def unknown_log_mode(conn: Connection) -> dict:
    """Is the visitor log on? {"enabled", "updated", "updated_by"}, read from the database each time."""
    return _switch(conn, UNKNOWN_LOG, config.UNKNOWN_LOG_ENABLED_DEFAULT)


def unknown_log_enabled(conn: Connection) -> bool:
    return unknown_log_mode(conn)["enabled"]


def set_unknown_log_enabled(conn: Connection, enabled: bool, reason: str, user: str) -> bool:
    """Switch the visitor log on or off; audited (enable_unknown_log / disable_unknown_log). False if it already was.
    The camera workers notice within UNKNOWN_SWITCH_SECONDS; visits already recorded stay."""
    require_reason(reason)
    with conn:
        if unknown_log_enabled(conn) == enabled:
            return False
        _set_switch(conn, UNKNOWN_LOG, enabled, user)
        _audit(conn, user, "enable_unknown_log" if enabled else "disable_unknown_log", f"settings:{UNKNOWN_LOG}",
               {UNKNOWN_LOG: ["off" if enabled else "on", "on" if enabled else "off"]}, reason)
    return True


def add_unknown_visit(conn: Connection, camera_id: int, first_seen: datetime, last_seen: datetime,
                      tracks: int, score: float, closest_code: str | None, liveness: float | None,
                      face_px: int | None, snapshot_path: str | None, retry: bool = False) -> int:
    """A camera worker's new visit; returns its id. `retry`: an earlier try failed, maybe after the database had
    committed it (SQL Server's connection lost during COMMIT); the identical visit (camera, first seen, score,
    snapshot) is looked for first and, if there, brought up to date instead of written twice."""
    with conn:
        if retry:
            same = conn.execute(
                "SELECT id FROM unknown_visits WHERE camera_id = ? AND first_seen = ? AND best_score = ? AND "
                + ("snapshot_path IS NULL" if snapshot_path is None else "snapshot_path = ?"),
                (camera_id, _ts(first_seen), score, *(() if snapshot_path is None else (snapshot_path,)))).fetchone()
            if same:
                conn.execute("UPDATE unknown_visits SET last_seen = ?, tracks = ? WHERE id = ?",
                             (_ts(last_seen), tracks, same["id"]))
                return same["id"]
        cur = conn.execute(
            "INSERT INTO unknown_visits (camera_id, first_seen, last_seen, tracks, best_score, closest_code, liveness, "
            "face_px, snapshot_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (camera_id, _ts(first_seen), _ts(last_seen), tracks, score, closest_code, liveness, face_px,
             snapshot_path))
    return cur.lastrowid


def update_unknown_visit(conn: Connection, visit_id: int, last_seen: datetime, tracks: int) -> None:
    """A visit's latest last-seen time and pass count (the worker, when a track ends or merges into it)."""
    with conn:
        conn.execute("UPDATE unknown_visits SET last_seen = ?, tracks = ? WHERE id = ?",
                     (_ts(last_seen), tracks, visit_id))


def delete_unknown_visit(conn: Connection, visit_id: int) -> bool:
    """The worker withdraws its own visit (the track was recognised after all): only while nobody has reviewed it.
    Not audited: no one has acted on it. True if it was deleted."""
    with conn:
        cur = conn.execute("DELETE FROM unknown_visits WHERE id = ? AND status = 'new' AND reviewed IS NULL",
                           (visit_id,))
    return cur.rowcount == 1


def unknown_visit(conn: Connection, visit_id: int) -> Row | None:
    return conn.execute(UNKNOWN_COLUMNS + "WHERE v.id = ?", (visit_id,)).fetchone()


def review_unknown_visit(conn: Connection, visit_id: int, status: str, person_id: int | None,
                         note: str | None, reason: str, user: str) -> bool:
    """Review a visit: its status (new, dismissed, or identified with the person it was), and its note (None =
    unchanged, blank = cleared). Audited (review_unknown, the old and new values). False if nothing changes."""
    require_reason(reason)
    if status not in VISIT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(VISIT_STATUSES)}")
    if (status == "identified") != (person_id is not None):
        raise ValueError("a person goes with status identified, and only with it")
    with conn:
        old = unknown_visit(conn, visit_id)
        if old is None:
            raise LookupError(f"no unknown visitor with id {visit_id}")
        person = None if person_id is None else _person(conn, person_id)["employee_code"]
        new_note = old["note"] if note is None else (note.strip() or None)
        changes = {k: [before, after] for k, before, after in (
            ("status", old["status"], status), ("person", old["person_code"], person), ("note", old["note"], new_note))
            if before != after}
        if not changes:
            return False
        conn.execute("UPDATE unknown_visits SET status = ?, person_id = ?, note = ?, reviewed = ?, reviewed_by = ? "
                     "WHERE id = ?", (status, person_id, new_note, _ts(datetime.now()), user, visit_id))
        _audit(conn, user, "review_unknown", f"unknown:{visit_id}",
               {"camera": old["camera"], "first_seen": old["first_seen"], **changes}, reason)
    return True


def delete_unknown_visits_before(conn: Connection, before: datetime) -> list[str | None]:
    """Delete the visits first seen before `before` (the nightly retention), whatever their status, in one
    transaction; returns their snapshot paths (None: no snapshot) for the caller to delete the files."""
    with conn:
        rows = conn.execute("SELECT snapshot_path FROM unknown_visits WHERE first_seen < ?", (_ts(before),)).fetchall()
        if rows:
            conn.execute("DELETE FROM unknown_visits WHERE first_seen < ?", (_ts(before),))
    return [r["snapshot_path"] for r in rows]
