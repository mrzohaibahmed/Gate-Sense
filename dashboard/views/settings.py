import pandas as pd
import streamlit as st

from app import config
from app.camera import mask_source

st.title("Settings")
st.caption("Read-only. Change values in app/config.py, then restart the dashboard and the camera workers. "
           "Cameras are added and edited on the Camera settings page.")

GROUPS = {
    "Recognition": [
        ("MATCH_THRESHOLD", "minimum cosine similarity to accept a match"),
        ("MATCH_MARGIN", "top person must beat the second-best by this much"),
        ("MODEL_PACK", "InsightFace model pack"),
    ],
    "Tracking and vote": [
        ("BEST_FRAMES_PER_TRACK", "frames embedded and kept per tracked face"),
        ("VOTE_MIN_AGREE", "kept frames one person must win to be recorded"),
        ("TRACK_LOST_SECONDS", "a track not seen for this long is closed"),
    ],
    "Attendance": [
        ("COOLDOWN_SECONDS", "same person, same camera ignored for this long"),
    ],
    "Processing": [
        ("PROCESS_FPS", "frames processed per second"),
        ("FRAME_WIDTH", "frames are resized to this width"),
        ("DET_THRESHOLD", "minimum detector confidence"),
    ],
    "Quality filter": [
        ("MIN_FACE_PX", "minimum face width in pixels"),
        ("MIN_BLUR_VAR", "minimum sharpness (variance of Laplacian)"),
        ("MAX_YAW_RATIO", "maximum sideways turn"),
        ("PITCH_RANGE", "allowed up/down tilt"),
        ("ENROLL_MIN_PHOTOS", "warn when a person has fewer photos"),
    ],
    "Enrollment photos": [
        ("ENROLL_MAX_YAW", "degrees a face may be turned left/right"),
        ("ENROLL_MAX_PITCH", "degrees a face may be tilted up/down"),
        ("ENROLL_DET_THRESHOLD", "face detector confidence at enrollment"),
        ("ENROLL_MIN_BRIGHTNESS", "minimum face brightness (0-255)"),
    ],
}

for title, items in GROUPS.items():
    st.subheader(title)
    st.dataframe(
        pd.DataFrame([{"Setting": key, "Value": str(getattr(config, key)), "Meaning": meaning}
                      for key, meaning in items]),
        hide_index=True,
    )

st.subheader("Cameras")
st.dataframe(
    pd.DataFrame([{"Name": c["name"], "Direction": c["direction"], "Source": mask_source(c["source"]),
                   "Mirrored view": bool(c.get("mirror"))}
                  for c in config.CAMERAS]),
    hide_index=True,
)
