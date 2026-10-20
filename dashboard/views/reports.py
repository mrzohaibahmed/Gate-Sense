import calendar
from datetime import date

import streamlit as st

from app import reports
from dashboard.common import connection

st.title("Reports")

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
mode = st.segmented_control("Report", ["Daily", "Monthly"], default="Daily")

if mode == "Monthly":
    today = date.today()
    c1, c2 = st.columns(2)
    year = c1.number_input("Year", min_value=2020, max_value=today.year, value=today.year, step=1)
    month = c2.selectbox("Month", range(1, 13), index=today.month - 1, format_func=lambda m: calendar.month_name[m])
    if date(int(year), month, 1) > today:
        st.info("That month hasn't started yet.")
        st.stop()
    with connection() as conn:
        summary, detail = reports.monthly_report(conn, int(year), month)
    label = f"{int(year)}-{month:02d}"

    st.subheader(f"Summary {label}")
    st.dataframe(summary, hide_index=True)
    st.subheader("Daily detail")
    st.dataframe(detail, hide_index=True)

    c1, c2, c3 = st.columns(3)
    c1.download_button("Excel (summary + detail)", reports.to_excel({"Summary": summary, "Daily": detail}),
                       f"gatesense_{label}.xlsx", XLSX, icon=":material/download:")
    c2.download_button("CSV summary", reports.to_csv(summary), f"gatesense_{label}_summary.csv", "text/csv",
                       icon=":material/download:")
    c3.download_button("CSV detail", reports.to_csv(detail), f"gatesense_{label}_daily.csv", "text/csv",
                       icon=":material/download:")
else:
    day = st.date_input("Date", value=date.today(), max_value=date.today())
    with connection() as conn:
        df = reports.daily_report(conn, day)
    st.dataframe(df, hide_index=True)
    c1, c2 = st.columns(2)
    c1.download_button("Excel", reports.to_excel({day.isoformat(): df}), f"gatesense_{day}.xlsx", XLSX,
                       icon=":material/download:")
    c2.download_button("CSV", reports.to_csv(df), f"gatesense_{day}.csv", "text/csv", icon=":material/download:")
