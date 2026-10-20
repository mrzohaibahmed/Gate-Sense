import json
from datetime import date, datetime

import pandas as pd
import streamlit as st

from app import config, db, reports
from dashboard.common import connection, flash, person_label, show_flash

st.title("Corrections")
st.caption("Nothing is deleted: events can be added, re-timed, voided or restored. "
           "Every change needs a reason and is written to the audit log.")
show_flash()
USER = config.DASHBOARD_USER

add_tab, edit_tab, audit_tab = st.tabs(["Add event", "Edit or void event", "Audit log"])

with connection() as conn:
    # Widget options must be copyable, so use dicts rather than sqlite3.Row.
    people = [dict(p) for p in db.list_persons(conn) if p["active"]]
    cameras = [dict(c) for c in db.list_cameras(conn)]

with add_tab:
    if not people:
        st.info("No active people.")
    else:
        with st.form("add_event", clear_on_submit=True):
            person = st.selectbox("Person", people, format_func=person_label)
            camera = st.selectbox("Camera", cameras, format_func=lambda c: f"{c['name']} ({c['direction']})")
            c1, c2 = st.columns(2)
            day = c1.date_input("Date", value=date.today(), max_value=date.today())
            at = c2.time_input("Time", value=None, step=60)
            reason = st.text_input("Reason (required)", placeholder="e.g. forgot to look at the exit camera")
            if st.form_submit_button("Add event"):
                if at is None:
                    st.error("Pick a time.")
                else:
                    try:
                        with connection() as conn:
                            eid = db.add_manual_event(conn, person["id"], camera["id"], datetime.combine(day, at),
                                                      reason, USER)
                    except ValueError as e:
                        st.error(str(e).capitalize())
                    else:
                        flash(f"Added manual {camera['direction']} event #{eid} for {person['name']}.")
                        st.rerun()

with edit_tab:
    day = st.date_input("Day", value=date.today(), max_value=date.today(), key="edit_day")
    with connection() as conn:
        events = [dict(e) for e in db.events_on(conn, day)]
    if not events:
        st.info("No events that day.")
    else:
        def label(e) -> str:
            flags = (" [manual]" if e["manual"] else "") + (" [VOIDED]" if e["voided"] else "")
            return f"#{e['id']}  {e['timestamp'][11:]}  {e['direction']:<3} {e['name']} ({e['camera']}){flags}"

        event = st.selectbox("Event", events, format_func=label)
        current = datetime.fromisoformat(event["timestamp"])
        with st.form("edit_event"):
            new_time = st.time_input("New time", value=current.time(), step=60)
            reason = st.text_input("Reason (required)")
            c1, c2 = st.columns(2)
            change = c1.form_submit_button("Change time")
            toggle = c2.form_submit_button("Restore event" if event["voided"] else "Void event")
            if change or toggle:
                try:
                    with connection() as conn:
                        if change:
                            db.edit_event_time(conn, event["id"], datetime.combine(current.date(), new_time),
                                               reason, USER)
                        else:
                            db.set_event_voided(conn, event["id"], not event["voided"], reason, USER)
                except ValueError as e:
                    st.error(str(e).capitalize())
                else:
                    flash(f"Event #{event['id']} {'re-timed' if change else 'restored' if event['voided'] else 'voided'}.")
                    st.rerun()

with audit_tab:
    with connection() as conn:
        entries = db.audit_entries(conn)
    if not entries:
        st.info("No changes yet.")
    else:
        df = pd.DataFrame(
            [{
                "When": e["timestamp"],
                "User": e["user"],
                "Action": e["action"],
                "Target": e["target"],
                "Details": json.dumps(json.loads(e["details"]), ensure_ascii=False),
                "Reason": e["reason"],
            }
            for e in entries
        ])
        st.dataframe(df, hide_index=True)
        st.download_button("CSV", reports.to_csv(df), "gatesense_audit_log.csv", "text/csv",
                           icon=":material/download:")
