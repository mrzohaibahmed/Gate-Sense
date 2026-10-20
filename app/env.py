"""Loads .env from the project root.

Variables already set in the real environment always win; .env only fills in
what is missing. Never print or log the values read here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app import config


def load_env(path: Path | None = None) -> None:
    """Load ``path`` (default ``config.ENV_PATH``) without overriding existing variables."""
    path = path or config.ENV_PATH
    if path.is_file():
        load_dotenv(path, override=False)


def admin_password_source() -> str | None:
    """Load .env, then say where the admin password came from: "environment", ".env" or None.

    Only the source is returned, never the password itself.
    """
    name = config.DASHBOARD_PASSWORD_ENV
    if os.environ.get(name):
        return "environment"
    load_env()
    return ".env" if os.environ.get(name) else None
