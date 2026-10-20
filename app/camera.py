"""Opening video sources: a webcam index, a video file path or an RTSP URL."""

import re
import sys

import cv2


def parse_source(value: str | int) -> str | int:
    """'0' -> 0 (webcam index); anything else stays a path or URL."""
    if isinstance(value, int):
        return value
    return int(value) if value.strip().isdigit() else value


def is_webcam(source: str | int) -> bool:
    return isinstance(source, int)


def mask_source(source: str | int) -> str:
    """Hide the password in rtsp://user:password@host URLs before showing or logging them."""
    return re.sub(r"(://[^:/@\s]+:)[^@\s]*@", r"\1***@", str(source))


def open_capture(source: str | int) -> cv2.VideoCapture:
    """Open a source; raise RuntimeError if it can't be opened."""
    if is_webcam(source) and sys.platform == "win32":
        cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)  # DirectShow opens webcams much faster on Windows
    else:
        cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"could not open video source {source!r}")
    return cap
