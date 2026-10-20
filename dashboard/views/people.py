import pandas as pd
import streamlit as st

from app import config, db
from dashboard.common import connection, flash, person_label, show_flash

st.title("People")
show_flash()

with connection() as conn:
    people = [dict(p) for p in db.list_persons(conn)]  # widget options must be copyable

if not people:
    st.info("No one enrolled yet. Add photos to known/<name>/ and run scripts\\enroll.py.")
    st.stop()

st.dataframe(
    pd.DataFrame(
        [{
            "Code": p["employee_code"],
            "Name": p["name"],
            "Department": p["department"] or "",
            "Shift": p["shift"] or "",
            "Active": bool(p["active"]),
        }
        for p in people
    ]),
    hide_index=True,
)
st.caption("New people are added by scripts\\enroll.py. Inactive people are not recorded by the cameras.")

st.subheader("Edit a person")
person = st.selectbox("Person", people, format_func=person_label)
with st.form("edit_person"):
    name = st.text_input("Name", person["name"])
    c1, c2 = st.columns(2)
    department = c1.text_input("Department", person["department"] or "")
    shift = c2.text_input("Shift", person["shift"] or "")
    active = st.checkbox("Active (recorded by cameras)", bool(person["active"]))
    reason = st.text_input("Reason for the change (required)")
    if st.form_submit_button("Save"):
        try:
            with connection() as conn:
                db.update_person(conn, person["id"], name, department, shift, reason, config.DASHBOARD_USER)
                if active != bool(person["active"]):
                    db.set_person_active(conn, person["id"], active, reason, config.DASHBOARD_USER)
        except ValueError as e:
            st.error(str(e).capitalize())
        else:
            flash(f"Saved {person['employee_code']}.")
            st.rerun()
