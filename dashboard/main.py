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

from app import config  # noqa: E402
from dashboard import auth  # noqa: E402
from dashboard.common import claim_camera, manager  # noqa: E402

config.refresh_cameras()  # pick up cameras.json edited or deleted by hand, without a restart

st.set_page_config(page_title="GateSense", page_icon=":material/door_front:", layout="wide")

if not auth.require_login():
    st.stop()

nav = st.navigation(
    [
        st.Page("views/live.py", title="Live events", icon=":material/event_note:", default=True),
        st.Page("views/live_view.py", title="Live view", icon=":material/videocam:"),
        st.Page("views/today.py", title="Today", icon=":material/today:"),
        st.Page("views/reports.py", title="Reports", icon=":material/table_chart:"),
        st.Page("views/people.py", title="People", icon=":material/group:"),
        st.Page("views/enrollment.py", title="Enrollment", icon=":material/person_add:"),
        st.Page("views/cameras.py", title="Cameras", icon=":material/linked_camera:"),
        st.Page("views/camera_settings.py", title="Camera settings", icon=":material/video_settings:"),
        st.Page("views/corrections.py", title="Corrections", icon=":material/edit_note:"),
        st.Page("views/settings.py", title="Settings", icon=":material/settings:"),
    ]
)
if nav.title != "Enrollment":
    claim_camera(False)  # leaving the Enrollment page releases the browser camera
with st.sidebar:
    st.caption("GateSense")
    running = [s.camera for s in manager().statuses() if s.state != "stopped"]
    st.caption(f"Workers running: {', '.join(running)}" if running else "No camera workers running")
    if st.button("Log out", icon=":material/logout:"):
        auth.logout()
nav.run()
