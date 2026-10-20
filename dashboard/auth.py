"""Single admin login. The password comes from the environment or .env, never from code."""

import hmac
import os
import time

import streamlit as st

from app import config
from app.env import admin_password_source


def require_login() -> bool:
    """Show the login form until the admin password is entered; True once logged in."""
    if admin_password_source() is None:  # also loads .env if the variable isn't set yet
        st.error(
            f"No admin password set. Set `{config.DASHBOARD_PASSWORD_ENV}` in the environment, or put "
            f"`{config.DASHBOARD_PASSWORD_ENV}=...` in `.env` in the project root (see `.env.example`), "
            "and restart the dashboard."
        )
        return False
    expected = os.environ[config.DASHBOARD_PASSWORD_ENV]
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
