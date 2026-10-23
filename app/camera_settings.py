"""Camera configuration as edited in the dashboard's Settings > Cameras (through /api/cameras).

Cameras are saved to config.CAMERAS_FILE (cameras.json in the project folder):

    {"cameras": [{"name": "gate1", "direction": "IN", "mirror": false, "type": "hikvision",
                  "ip": "192.168.1.101", "port": 554, "user": "gatesense", "channel": 1, "stream": "main",
                  "source": "rtsp://gatesense:${GATESENSE_CAM_GATE1_PASS}@192.168.1.101:554/Streaming/Channels/101"}]}

`source` is what the worker uses; the other fields let the page show the form again.
Passwords are never in this file: a Hikvision camera's password goes to .env under
GATESENSE_CAM_<NAME>_PASS and the source refers to it as ${...} (see app/sources.py).
Every change is written to the audit log (without the password).
"""

import ipaddress
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values, set_key

from app import config, db, sources
from app.camera import mask_source
from app.fileutil import atomic_write_bytes

TYPES = {
    "webcam": "Webcam (USB or built-in)",
    "hikvision": "Hikvision IP camera",
    "rtsp": "Other IP camera (RTSP URL)",
    "file": "Video file",
}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
DEFAULT_REASON = "changed in Settings > Cameras"


def password_var(name: str) -> str:
    """The .env variable holding this camera's password, e.g. gate1 -> GATESENSE_CAM_GATE1_PASS."""
    return "GATESENSE_CAM_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_PASS"


def hikvision_source(ip: str, port: int, user: str, channel: int, stream: str, var: str) -> str:
    """Hikvision RTSP URL: channel 1 main stream = /Streaming/Channels/101, sub stream = 102."""
    return (f"rtsp://{quote(user, safe='')}:${{{var}}}@{ip}:{port}"
            f"/Streaming/Channels/{channel}{'01' if stream == 'main' else '02'}")


def _type_of(source) -> str:
    if isinstance(source, int):
        return "webcam"
    return "rtsp" if str(source).lower().startswith(("rtsp://", "rtsps://")) else "file"


def entries(path: Path | None = None) -> list[dict]:
    """The editable camera list: cameras.json if it exists, else the defaults from app/config.py."""
    path = path or config.CAMERAS_FILE
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))["cameras"]
        except (OSError, ValueError, KeyError, TypeError):
            pass  # config.load_cameras already warned; start again from the defaults
    out = []
    for cam in config.DEFAULT_CAMERAS:  # no file: the defaults, not a possibly stale in-memory list
        e = {"name": cam["name"], "direction": cam["direction"], "mirror": bool(cam.get("mirror")),
             "type": _type_of(cam["source"]), "source": cam["source"]}
        if e["type"] == "webcam":
            e["index"] = cam["source"]
        elif e["type"] == "rtsp":
            e["url"] = cam["source"]
        else:
            e["path"] = cam["source"]
        out.append(e)
    return out


def _literal_password(url: str) -> bool:
    """Does an RTSP URL carry a typed-in password (user:secret@) rather than a ${VAR} placeholder?"""
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/@]*)@", url, re.I)
    if not m or ":" not in m.group(1):
        return False
    password = m.group(1).split(":", 1)[1]
    return bool(password) and not re.fullmatch(r"(\$\{[A-Za-z_][A-Za-z0-9_]*\})+", password)


def public_source(source):
    """The source as it may be shown: ${VAR} placeholders kept (they aren't secrets), a typed-in password masked."""
    if isinstance(source, int):
        return source
    return mask_source(source) if _literal_password(str(source)) else str(source)


def build(entry: dict, others: list[dict]) -> dict:
    """Validate a form entry and compute its `source`; raises ValueError with a message for the page."""
    e = {k: v for k, v in entry.items() if v is not None}
    name = str(e.get("name", "")).strip()
    if not NAME_RE.fullmatch(name) or name.lower() in WINDOWS_RESERVED:
        raise ValueError("camera name must start with a letter or digit and use only letters, digits, '-' or '_' "
                         "(max 32), e.g. gate1 or main-entrance")
    if any(o["name"].lower() == name.lower() for o in others):
        raise ValueError(f"there is already a camera called {name!r}")
    if e.get("direction") not in ("IN", "OUT"):
        raise ValueError("direction must be IN (entrance) or OUT (exit)")
    kind = e.get("type")
    out = {"name": name, "direction": e["direction"], "mirror": bool(e.get("mirror")), "type": kind}
    if e.get("roi"):
        out["roi"] = check_roi(e["roi"])

    if kind == "webcam":
        index = int(e.get("index", 0))
        if not 0 <= index <= 9:
            raise ValueError("webcam number must be 0 to 9 (0 is the first webcam)")
        out.update(index=index, source=index)
    elif kind == "hikvision":
        ip = str(e.get("ip", "")).strip()
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            if not HOST_RE.match(ip):
                raise ValueError("enter the camera's IP address, e.g. 192.168.1.101") from None
        port, channel = int(e.get("port", 554)), int(e.get("channel", 1))
        user = str(e.get("user", "")).strip()
        if not 1 <= port <= 65535:
            raise ValueError("RTSP port must be 1 to 65535 (Hikvision default: 554)")
        if not user:
            raise ValueError("enter the camera user name (create a separate user for GateSense on the camera)")
        if not 1 <= channel <= 64:
            raise ValueError("channel must be 1 to 64 (1 for a single camera)")
        stream = e.get("stream", "main")
        if stream not in ("main", "sub"):
            raise ValueError("stream must be main or sub")
        var = password_var(name)
        out.update(ip=ip, port=port, user=user, channel=channel, stream=stream,
                   source=hikvision_source(ip, port, user, channel, stream, var))
    elif kind == "rtsp":
        url = str(e.get("url", "")).strip()
        if not url.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("the URL must start with rtsp://")
        if _literal_password(url):
            raise ValueError("don't type the password into the URL: write it as ${VARIABLE} (for example "
                             "rtsp://user:${GATESENSE_CAM_PASS}@ip:554/...) and set the variable in .env")
        out.update(url=url, source=url)
    elif kind == "file":
        path = str(e.get("path", "")).strip().strip('"')
        if not path:
            raise ValueError("enter the path of the video file")
        out.update(path=path, source=path)
    else:
        raise ValueError(f"unknown camera type {kind!r}")
    return out


def check_roi(roi) -> dict:
    """Region of interest as fractions of the frame: {"x", "y", "w", "h"} in 0..1, inside the frame.

    Stored with the camera; the worker doesn't apply it yet.
    """
    try:
        x, y, w, h = (float(roi[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("roi needs x, y, w and h as fractions of the frame (0 to 1)") from None
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.0001 and y + h <= 1.0001):
        raise ValueError("roi must lie inside the frame: x, y from 0, w, h above 0, x + w and y + h at most 1")
    return {"x": x, "y": y, "w": w, "h": h}


def save(cams: list[dict], path: Path | None = None) -> None:
    """Write cameras.json atomically and reload config.CAMERAS in this process."""
    path = path or config.CAMERAS_FILE
    data = {"_comment": "GateSense cameras, edited in the dashboard's Settings > Cameras. "
                        "No passwords here: they are in .env (see app/sources.py).",
            "cameras": cams}
    atomic_write_bytes(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    config.CAMERAS = config.load_cameras(path)


# --- Passwords in .env -------------------------------------------------------------------------


def password_status(var: str) -> str | None:
    """Where the password is set: "environment", ".env" or None. Never returns the value."""
    import os

    if os.environ.get(var):
        return "environment"
    if config.ENV_PATH.is_file() and dotenv_values(config.ENV_PATH).get(var):
        return ".env"
    return None


def set_env_value(key: str, value: str) -> None:
    """Add or replace one line in .env (other lines are kept); verified by reading it back."""
    if not value:
        raise ValueError("the password is empty")
    if any(c in value for c in "\r\n\0"):
        raise ValueError("the password can't contain line breaks")
    path = config.ENV_PATH
    before = path.read_bytes() if path.is_file() else None
    set_key(path, key, value, quote_mode="always")
    if dotenv_values(path).get(key) != value:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)
        raise ValueError("this password can't be stored in .env; set it as an environment variable instead")


# --- Changes (audited) ------------------------------------------------------------------------------


def _check_stopped(name: str, running: set[str]) -> None:
    if name in running:
        raise ValueError(f"the {name} camera is running: stop it on the Cameras page first")


def upsert(entry: dict, user: str, conn, running: set[str] = frozenset(), password: str | None = None,
           reason: str = "") -> dict:
    """Add a camera, or update the one with the same name. `password` (Hikvision) goes to .env."""
    cams = entries()
    old = next((c for c in cams if c["name"] == entry.get("name")), None)
    if old:
        _check_stopped(old["name"], running)
        if "roi" not in entry and old.get("roi"):  # the dashboard form has no ROI: keep the one set by the API
            entry = {**entry, "roi": old["roi"]}
    others = [c for c in cams if c is not old]
    new = build(entry, others)
    if password:
        if new["type"] != "hikvision":
            raise ValueError("a password can only be entered for a Hikvision camera; for other URLs use ${VARIABLE}")
        set_env_value(password_var(new["name"]), password)
    cams = [new if c is old else c for c in cams] if old else cams + [new]
    save(cams)
    changes = {k: [old.get(k), v] for k, v in new.items() if old and old.get(k) != v} if old else None
    details = {"type": new["type"], "direction": new["direction"], "source": mask_source(new["source"]),
               "password_changed": bool(password)}
    if changes:
        details["changes"] = {k: [mask_source(a) if k == "source" else a, mask_source(b) if k == "source" else b]
                              for k, (a, b) in changes.items()}
    db.log_change(conn, user, "edit_camera" if old else "add_camera", f"camera:{new['name']}", details,
                  reason.strip() or DEFAULT_REASON)
    return new


def delete(name: str, user: str, conn, running: set[str] = frozenset(), reason: str = "") -> None:
    """Remove a camera from the list. Its attendance history stays; its .env password is left alone."""
    _check_stopped(name, running)
    cams = entries()
    if not any(c["name"] == name for c in cams):
        raise ValueError(f"no camera called {name!r}")
    if len(cams) == 1:
        raise ValueError("keep at least one camera")
    gone = next(c for c in cams if c["name"] == name)
    save([c for c in cams if c["name"] != name])
    db.log_change(conn, user, "delete_camera", f"camera:{name}",
                  {"type": gone["type"], "source": mask_source(gone["source"])}, reason.strip() or DEFAULT_REASON)


def move(name: str, step: int) -> None:
    """Move a camera up (-1) or down (+1); the first camera is the default for recognize.py."""
    cams = entries()
    i = next(i for i, c in enumerate(cams) if c["name"] == name)
    j = min(max(i + step, 0), len(cams) - 1)
    cams[i], cams[j] = cams[j], cams[i]
    save(cams)


# --- Test connection ----------------------------------------------------------------------------------


def test_connection(source, timeout: float = 12.0, frames=None, extra: dict | None = None) -> dict:
    """Open the source briefly; returns {"ok", "width", "height", "fps", "codec", "frame", "error"} (error masked).

    fps is what the camera or file reports (None if it doesn't say; webcams often don't).

    `extra`: variables not saved yet, e.g. a password typed in the form. `frames` is injectable for tests
    (anything with latest()/stop()/codec/last_error).
    """
    from app.camera import open_capture
    from app.frame_source import FrameSource, kind, open_rtsp

    result = {"ok": False, "width": None, "height": None, "fps": None, "codec": None, "frame": None, "error": None}
    try:
        stream = sources.expand(source, extra)
    except sources.MissingVariables as e:
        result["error"] = str(e)
        return result
    k = kind(stream)
    if k == "file" and not Path(str(stream)).is_file():
        result["error"] = f"video file not found: {stream}"
        return result
    if frames is None:
        opener = open_rtsp if k == "rtsp" else open_capture
        frames = FrameSource(stream, opener=opener, log=lambda *_: None, backoff=(timeout,))
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            got = frames.latest()
            if got:
                # The reader stores the first frame a moment before the source passes on codec and fps.
                settle = time.monotonic() + 1.0
                while getattr(frames, "codec", "") is None and time.monotonic() < settle:
                    time.sleep(0.02)
                frame = got[1]
                result.update(ok=True, frame=frame.copy(), width=frame.shape[1], height=frame.shape[0],
                              codec=getattr(frames, "codec", None), fps=getattr(frames, "reported_fps", None))
                return result
            time.sleep(0.05)
        error = getattr(frames, "last_error", None)
        result["error"] = mask_source(error) if error else f"no picture within {timeout:g} s"
        return result
    finally:
        frames.stop()
