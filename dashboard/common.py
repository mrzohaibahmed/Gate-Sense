"""Shared helpers for the dashboard pages."""

import atexit
import base64
import sqlite3
import time
from contextlib import closing

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from app import config, db
from app.workers import WorkerManager


@st.cache_resource
def _camera_claims() -> dict[str, float]:
    """Browser sessions that have the Enrollment camera switched on: {session id: last seen}."""
    return {}


def claim_camera(on: bool) -> None:
    """Record whether this browser session has the Enrollment page camera switched on."""
    ctx = get_script_run_ctx()
    sid = ctx.session_id if ctx else "no-session"
    if on:
        _camera_claims()[sid] = time.time()
    else:
        _camera_claims().pop(sid, None)


def camera_claimed() -> bool:
    """Is the Enrollment camera on in any session? (Claims expire after ENROLL_CAMERA_CLAIM_SECONDS.)"""
    claims = _camera_claims()
    for sid, seen in list(claims.items()):
        if time.time() - seen > config.ENROLL_CAMERA_CLAIM_SECONDS:
            claims.pop(sid, None)
    return bool(claims)


@st.cache_resource
def manager() -> WorkerManager:
    """One worker manager per dashboard process; its workers are stopped when the dashboard exits.

    (Workers also watch the dashboard's PID and exit on their own if it dies without running atexit.)
    """
    mgr = WorkerManager()
    atexit.register(mgr.stop_all)
    return mgr


@st.cache_resource(show_spinner="Loading face models...")
def face_models():
    """Detector and recognizer for the Enrollment page, loaded once on first use."""
    from app.detector import Detector
    from app.recognizer import Recognizer

    return Detector(), Recognizer()


def connection() -> closing[sqlite3.Connection]:
    """A fresh connection per use (Streamlit reruns run on different threads)."""
    conn = db.connect(config.DB_PATH)
    db.ensure_cameras(conn, config.CAMERAS)
    return closing(conn)


@st.cache_data(max_entries=2000)
def thumbnail(rel_path: str | None) -> str | None:
    """Snapshot as a data URI for st.dataframe's ImageColumn; snapshots never change, so cache them."""
    if not rel_path:
        return None
    path = config.DATA_DIR / rel_path
    if not path.is_file():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def flash(message: str) -> None:
    """Show a success message after the next rerun."""
    st.session_state["flash"] = message


def show_flash() -> None:
    if msg := st.session_state.pop("flash", None):
        st.success(msg)


def person_label(row) -> str:
    label = f"{row['name']} ({row['employee_code']})"
    return label if row["active"] else f"{label} [inactive]"
