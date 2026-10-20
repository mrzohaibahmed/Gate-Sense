from datetime import date, datetime

import streamlit as st

from app import config, reports
from app.attendance import ABSENT, MISSING_ENTRY, MISSING_EXIT, PRESENT
from dashboard.common import connection

st.title("Today")


@st.fragment(run_every=config.LIVE_REFRESH_SECONDS * 6)
def today() -> None:
    day = date.today()
    with connection() as conn:
        df = reports.daily_report(conn, day)
    st.subheader(day.strftime("%A %d %B %Y"))
    if df.empty:
        st.info("No one enrolled yet. Run scripts\\enroll.py.")
        return
    counts = df["Status"].value_counts()
    cols = st.columns(4)
    for col, status in zip(cols, [PRESENT, MISSING_EXIT, MISSING_ENTRY, ABSENT]):
        col.metric(status.capitalize(), int(counts.get(status, 0)))
    st.dataframe(df.drop(columns=["Date"]), hide_index=True)
    st.caption(f"Updated {datetime.now():%H:%M:%S}. Missing exit = no OUT after the last IN; "
               "fix it on the Corrections page.")


today()
