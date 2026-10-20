"""Single admin login. The password comes from an environment variable, never from code."""

import hmac
import os
import time

import streamlit as st

from app import config


def require_login() -> bool:
    """Show the login form until the admin password is entered; True once logged in."""
    expected = os.environ.get(config.DASHBOARD_PASSWORD_ENV, "")
    if not expected:
        st.error(
            f"No admin password set. Set the `{config.DASHBOARD_PASSWORD_ENV}` environment variable "
            "and restart the dashboard."
        )
        return False
    if st.session_state.get("authenticated"):
        return True

    st.title("GateSense")
    with st.form("login"):
        password = st.text_input("Admin password", type="password", key="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        if hmac.compare_digest(password.encode(), expected.encode()):
            st.session_state.authenticated = True
            st.rerun()
        time.sleep(1)  # slow down guessing
        st.error("Wrong password.")
    return False


def logout() -> None:
    st.session_state.authenticated = False
    st.rerun()
