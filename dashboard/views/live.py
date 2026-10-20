from datetime import datetime

import pandas as pd
import streamlit as st

from app import config, db
from dashboard.common import connection, thumbnail

st.title("Live events")


@st.fragment(run_every=config.LIVE_REFRESH_SECONDS)
def feed() -> None:
    with connection() as conn:
        rows = db.recent_events(conn, config.LIVE_EVENT_LIMIT)
    if not rows:
        st.info("No events yet. Start a camera with scripts\\recognize.py.")
    else:
        df = pd.DataFrame(
            [{
                "Snapshot": thumbnail(r["snapshot_path"]),
                "Time": r["timestamp"],
                "Name": r["name"],
                "Code": r["employee_code"],
                "Direction": r["direction"],
                "Camera": r["camera"],
                "Score": None if r["manual"] else round(r["match_score"], 3),
                "Manual": bool(r["manual"]),
                "Voided": bool(r["voided"]),
            }
            for r in rows
        ])
        st.dataframe(
            df,
            hide_index=True,
            row_height=70,
            column_config={"Snapshot": st.column_config.ImageColumn("Snapshot", width="small")},
        )
    st.caption(f"Last {config.LIVE_EVENT_LIMIT} events. Updated {datetime.now():%H:%M:%S}, "
               f"refreshes every {config.LIVE_REFRESH_SECONDS} s.")


feed()
