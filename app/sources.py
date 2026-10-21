"""${VAR} placeholders in camera sources, so camera passwords stay out of app/config.py.

    "source": "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101"

Variables come from the real environment first, then from .env in the project
folder (config.ENV_PATH, read fresh each time, so editing .env needs no
dashboard restart). Values substituted into the user:password@ part of a URL
are percent-encoded, so characters like @ # / : work in passwords.

Expand only where the stream is opened (the worker and the probe); everywhere
else (dashboard, status files, database, logs) keeps the placeholder form, and
mask_source() hides a password in an expanded URL anyway.
"""

import os
import re
from urllib.parse import quote

from dotenv import dotenv_values

from app import config

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


class MissingVariables(ValueError):
    def __init__(self, names: list[str]):
        self.names = names
        listed = ", ".join(names)
        super().__init__(
            f"missing environment variable{'s' if len(names) > 1 else ''} {listed}: add "
            + ", ".join(f"{n}=..." for n in names)
            + f" to {config.ENV_PATH.name} in the project folder (or set it in the environment)"
        )


def placeholders(source) -> list[str]:
    return [] if isinstance(source, int) else PLACEHOLDER.findall(str(source))


def _values() -> dict[str, str | None]:
    """.env values (not loaded into os.environ, so nothing leaks into other processes' view)."""
    return dotenv_values(config.ENV_PATH) if config.ENV_PATH.is_file() else {}


def lookup(name: str, file_values: dict | None = None) -> str | None:
    """Environment first, then .env; empty counts as missing."""
    value = os.environ.get(name)
    if not value:
        value = (file_values if file_values is not None else _values()).get(name)
    return value or None


def missing_variables(source) -> list[str]:
    names = placeholders(source)
    if not names:
        return []
    values = _values()
    return sorted({n for n in names if lookup(n, values) is None})


def _userinfo_end(text: str) -> int:
    """Index just past the user:password@ part of a URL (0 if there is none)."""
    m = SCHEME.match(text)
    if not m:
        return 0
    authority_end = text.find("/", m.end())
    authority = text[m.end(): authority_end if authority_end >= 0 else len(text)]
    at = authority.rfind("@")
    return m.end() + at + 1 if at >= 0 else 0


def expand(source):
    """Source with ${VAR} replaced; webcam indexes and sources without placeholders are returned unchanged.

    Raises MissingVariables (a ValueError) listing every variable that isn't set.
    """
    if isinstance(source, int) or not placeholders(source):
        return source
    if missing := missing_variables(source):
        raise MissingVariables(missing)
    text = str(source)
    values = _values()
    userinfo_end = _userinfo_end(text)

    def substitute(m: re.Match) -> str:
        value = lookup(m.group(1), values)
        return quote(value, safe="") if m.start() < userinfo_end else value

    return PLACEHOLDER.sub(substitute, text)
