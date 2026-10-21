import time
from datetime import datetime

import streamlit as st

from app import config, sources
from app.camera import is_webcam, mask_source, parse_source
from app.workers import WorkerError, missing_file
from dashboard import live_panel
from dashboard.common import camera_claimed, manager

st.title("Cameras")
st.caption("One recognize.py worker per camera, run in the background with its log in data/logs/. "
           "Workers started here stop when the dashboard stops. Workers started from the command line "
           "are shown too and can be stopped here.")

BADGE = {"running": ":green[● running]", "starting": ":orange[● starting]",
         "not responding": ":red[● not responding]", "stopped": ":gray[○ stopped]"}


FEED = {"live": ":green-badge[● live]", "connecting": ":orange-badge[● connecting]",
        "reconnecting": ":orange-badge[● reconnecting]", "offline": ":red-badge[● offline]"}


def feed_lines(status) -> list[str]:
    """Camera feed health: badge, resolution, input vs processed fps, last frame age, reconnects, last error."""
    h = status.health if status.state != "stopped" else None
    state = (h or {}).get("state", "offline")
    if status.state == "stopped" or state == "stopped":
        state = "offline"
    lines = [FEED.get(state, FEED["offline"])]
    if not h:
        return lines
    if h.get("width"):
        lines.append(f"{h['width']}×{h['height']}")
    lines.append(f"input {h.get('input_fps') or 0:.1f} fps / processed {h.get('processed_fps') or 0:.1f} fps")
    if h.get("last_frame_at"):
        lines.append(f"last frame {max(0.0, time.time() - h['last_frame_at']):.1f} s ago")
    if h.get("kind") == "rtsp":
        lines.append(f"reconnects today: {h.get('reconnects_today', 0)}")
        if h.get("live_since") and state == "live":
            lines.append(f"live since {datetime.fromtimestamp(h['live_since']):%H:%M:%S}")
    if h.get("last_error") and state != "live":
        lines.append(f"last error: {mask_source(h['last_error'])}")
    return lines


def since(started: datetime | None) -> str:
    if not started:
        return ""
    mins = int((datetime.now() - started).total_seconds() // 60)
    took = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins} min"
    return f"since {started:%H:%M:%S} ({took})"


@st.fragment(run_every=1)
def live_views() -> None:
    running = [s for s in manager().statuses() if s.state != "stopped"]
    if running:
        st.subheader("Live view")
        cols = st.columns(min(len(running), 2))
        for i, status in enumerate(running):
            with cols[i % len(cols)], st.container(border=True):
                live_panel.render(status)


def start(name: str, source) -> None:
    if is_webcam(parse_source(str(source))) and camera_claimed():
        st.error("The camera is switched on on the Enrollment page (maybe in another browser tab), so the "
                 "webcam is busy. Turn off \"Use the camera\" there first.")
        return
    try:
        manager().start(name)
        st.toast(f"Starting {name}")
    except WorkerError as e:
        st.error(str(e))


@st.fragment(run_every=2)
def panel() -> None:
    mgr = manager()
    st.subheader("Workers")
    for cam in mgr.cameras:
        name = cam["name"]
        st_ = mgr.status(name)
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 4, 2])
            c1.markdown(f"**{name}** · {cam['direction']}  \n`{mask_source(cam['source'])}`")
            info = [BADGE[st_.state]]
            if st_.state != "stopped":
                info.append(since(st_.started))
                if st_.fps is not None and not st_.health:  # otherwise shown in the feed line below
                    info.append(f"{st_.fps:.1f} fps")
                info.append(f"pid {st_.pid}" + ("" if st_.managed else " · started outside this dashboard"))
            elif st_.detail:
                info.append(f"last run: {st_.detail}")
            c2.markdown("  \n".join(i for i in info if i))
            c2.markdown("  \n".join(feed_lines(st_)))
            if st_.warning:
                c2.error(st_.warning)
            if st_.state == "stopped" and (names := sources.missing_variables(cam["source"])):
                c2.error(f"{sources.MissingVariables(names)}. Then press Start.", icon=":material/key_off:")
            elif st_.state == "stopped" and (missing := missing_file(cam["source"])):
                c2.warning(f"Video file not found: `{missing}`. Put a clip there, or change this camera's "
                           "source in app/config.py.")
            if st_.state == "stopped":
                if c3.button("Start", key=f"start_{name}", icon=":material/play_arrow:", type="primary"):
                    start(name, cam["source"])
            elif c3.button("Stop", key=f"stop_{name}", icon=":material/stop:"):
                with st.spinner(f"Stopping {name}..."):
                    st.toast(f"{name}: {mgr.stop(name)}")
            with st.expander(f"Log (last {config.WORKER_LOG_LINES} lines)"):
                lines = mgr.log_tail(name)
                st.code("\n".join(lines) if lines else "(no log yet)", language=None)


live_views()
panel()
