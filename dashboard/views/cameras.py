from datetime import datetime

import streamlit as st

from app import config
from app.camera import mask_source
from app.workers import WorkerError
from dashboard.common import manager

st.title("Cameras")
st.caption("One recognize.py worker per camera, run in the background with its log in data/logs/. "
           "Workers started here stop when the dashboard stops. Workers started from the command line "
           "are shown too and can be stopped here.")

BADGE = {"running": ":green[● running]", "starting": ":orange[● starting]",
         "not responding": ":red[● not responding]", "stopped": ":gray[○ stopped]"}


def since(started: datetime | None) -> str:
    if not started:
        return ""
    mins = int((datetime.now() - started).total_seconds() // 60)
    took = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins} min"
    return f"since {started:%H:%M:%S} ({took})"


@st.fragment(run_every=2)
def panel() -> None:
    mgr = manager()
    for cam in mgr.cameras:
        name = cam["name"]
        st_ = mgr.status(name)
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 4, 2])
            c1.markdown(f"**{name}** · {cam['direction']}  \n`{mask_source(cam['source'])}`")
            info = [BADGE[st_.state]]
            if st_.state != "stopped":
                info.append(since(st_.started))
                if st_.fps is not None:
                    info.append(f"{st_.fps:.1f} fps")
                info.append(f"pid {st_.pid}" + ("" if st_.managed else " · started outside this dashboard"))
            elif st_.detail:
                info.append(f"last run: {st_.detail}")
            c2.markdown("  \n".join(i for i in info if i))
            if st_.state == "stopped":
                if c3.button("Start", key=f"start_{name}", icon=":material/play_arrow:", type="primary"):
                    try:
                        mgr.start(name)
                        st.toast(f"Starting {name}")
                    except WorkerError as e:
                        st.error(str(e))
            elif c3.button("Stop", key=f"stop_{name}", icon=":material/stop:"):
                with st.spinner(f"Stopping {name}..."):
                    st.toast(f"{name}: {mgr.stop(name)}")
            with st.expander(f"Log (last {config.WORKER_LOG_LINES} lines)"):
                lines = mgr.log_tail(name)
                st.code("\n".join(lines) if lines else "(no log yet)", language=None)


panel()
