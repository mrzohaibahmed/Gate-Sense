"""One running camera's live frame and recent decisions (Live view page and Cameras page)."""

import pandas as pd
import streamlit as st

from app import live
from app.workers import WorkerStatus

KINDS = {
    "match": "✅ match", "unknown": "❌ unknown", "skip": "⏭ skip", "event": "📝 event",
    "cool-down": "⏸ cool-down", "not saved": "⚠ not saved", "closed": "… closed",
}


def render(status: WorkerStatus) -> None:
    view = live.read_view(status.camera)
    message = live.frame_message(view.frame_age, view.waiting_for)

    parts = [f"**{status.camera}**"]
    if status.fps is not None:
        parts.append(f"{status.fps:.1f} fps")
    if view.frame_age is not None:
        parts.append(f"frame {view.frame_age:.1f} s old")
    st.markdown(" · ".join(parts))
    if warning := status.warning or view.warning:
        st.error(warning, icon=":material/videocam_off:")
    if message:
        st.warning(message, icon=":material/hourglass_empty:" if message == live.WAITING else ":material/warning:")
    if view.image:
        st.image(view.image, caption=None if message is None else "last frame received")

    decisions = list(reversed(view.decisions))  # newest first
    if decisions:
        st.dataframe(
            pd.DataFrame([{
                "Time": d["time"], "Track": d["track"], "Decision": KINDS.get(d["kind"], d["kind"]),
                "Name": d["name"] or "", "Score": d["score"], "Second": d["second"],
                "Face px": d.get("face_px"), "Detail": d["detail"],  # face_px: camera pixels (absent in old files)
            } for d in decisions]),
            hide_index=True,
            column_config={"Face px": st.column_config.NumberColumn(
                "Face px", format="%d", help="Face width in camera pixels (full-resolution frame)")},
        )
    else:
        st.caption("No decisions yet: no face has been tracked on this camera.")
