"""GateSense dashboard entry point.

Start it with the launcher, which binds to localhost:
    set GATESENSE_ADMIN_PASSWORD=<password>
    .venv\\Scripts\\python scripts\\dashboard.py
"""

import sys
from pathlib import Path

# Project root first, so `app` is the GateSense package (Streamlit puts dashboard/ on the path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from dashboard import auth  # noqa: E402

st.set_page_config(page_title="GateSense", page_icon=":material/door_front:", layout="wide")

if not auth.require_login():
    st.stop()

nav = st.navigation(
    [
        st.Page("views/live.py", title="Live events", icon=":material/videocam:", default=True),
        st.Page("views/today.py", title="Today", icon=":material/today:"),
        st.Page("views/reports.py", title="Reports", icon=":material/table_chart:"),
        st.Page("views/people.py", title="People", icon=":material/group:"),
        st.Page("views/corrections.py", title="Corrections", icon=":material/edit_note:"),
    ]
)
with st.sidebar:
    st.caption("GateSense")
    if st.button("Log out", icon=":material/logout:"):
        auth.logout()
nav.run()
