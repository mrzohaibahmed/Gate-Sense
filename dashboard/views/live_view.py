import streamlit as st

from dashboard import live_panel
from dashboard.common import manager

st.title("Live view")
st.caption("Each running camera's latest processed frame with face boxes, track, name or unknown and score, "
           "and its last 20 decisions, including unknown faces and quality skips.")


@st.fragment(run_every=1)
def views() -> None:
    running = [s for s in manager().statuses() if s.state != "stopped"]
    if not running:
        st.info("No camera is running. Start one on the Cameras page.")
        return
    for status in running:
        with st.container(border=True):
            live_panel.render(status)


views()
