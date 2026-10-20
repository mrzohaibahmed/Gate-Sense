"""Shared helpers for the dashboard pages."""

import base64
import sqlite3
from contextlib import closing

import streamlit as st

from app import config, db


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
