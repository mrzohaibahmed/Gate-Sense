"""Reads .env from the project root.

Variables already set in the real environment always win; .env only fills in
what is missing. Never print or log the values read here.

Only the admin password is copied into os.environ. Camera passwords (${VAR} in
camera sources) are read from .env fresh each time a camera starts
(app/sources.py); loading them into the app's environment would make the
workers it starts inherit stale copies, which would then win over later .env edits.
"""

import os
from pathlib import Path

from dotenv import dotenv_values

from app import config


def load_env(path: Path | None = None, keys: tuple[str, ...] | None = None) -> None:
    """Copy `keys` (default: the admin password) from .env into os.environ, never overriding existing ones."""
    path = path or config.ENV_PATH
    if not path.is_file():
        return
    keys = keys or (config.DASHBOARD_PASSWORD_ENV,)
    values = dotenv_values(path)
    for key in keys:
        if values.get(key) and not os.environ.get(key):
            os.environ[key] = values[key]


def admin_password_source() -> str | None:
    """Load the admin password from .env if needed, then say where it came from: "environment", ".env" or None.

    Only the source is returned, never the password itself.
    """
    name = config.DASHBOARD_PASSWORD_ENV
    if os.environ.get(name):
        return "environment"
    load_env()
    return ".env" if os.environ.get(name) else None
