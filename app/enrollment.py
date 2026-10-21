"""Enrollment, shared by scripts/enroll.py and the dashboard's Enrollment page.

A person's photos live in known/<employee code>/. Each photo is checked
(exactly one face that passes the quality filter) and embedded; all accepted
embeddings go into data/gallery.npz under the employee code, and the persons
table gets a row for everyone in the gallery.

- enroll_all: full rebuild from every folder (the CLI). People whose folder is
  gone are dropped from the gallery and deactivated, as before.
- enroll_one: re-embed one person's folder and replace just their entry (the
  dashboard, after adding or deleting photos).
- delete_face_data: remove a person's photos and embeddings, keep their
  attendance history, write an audit entry.
"""

import json
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from app import config, db, enroll_quality
from app.enroll_quality import QualityResult
from app.fileutil import atomic_write_bytes
from app.gallery import Gallery
from app.images import read_image, write_image

CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}

# Serializes gallery read-modify-write within one process (the dashboard can have several sessions).
_gallery_lock = threading.Lock()


def validate_code(code: str) -> str:
    """Employee codes double as folder names, so keep them filesystem-safe."""
    code = (code or "").strip()
    if not CODE_RE.fullmatch(code) or code.split(".")[0].lower() in WINDOWS_RESERVED:
        raise ValueError("employee code must start with a letter or digit and use only letters, digits, '_', '-' "
                         "or '.' (max 64)")
    return code


def person_dir(code: str) -> Path:
    return config.KNOWN_DIR / validate_code(code)


def photo_paths(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in config.IMAGE_EXTS)


@dataclass
class PhotoCheck:
    reason: str | None  # short technical reason it was refused (CLI, logs); None = accepted
    embedding: np.ndarray | None = None
    quality: QualityResult | None = None  # all the numbers, and `quality.reason`: the plain message

    @property
    def accepted(self) -> bool:
        return self.reason is None

    @property
    def message(self) -> str:
        """Plain message for the person enrolling ("" when accepted)."""
        return self.quality.reason if self.quality else (self.reason or "")


def check_photo(img: np.ndarray | None, detector, recognizer) -> PhotoCheck:
    """Run the enrollment quality rules (app/enroll_quality.py); embed the face if they all pass."""
    q = enroll_quality.check_photo(img, detector)
    if not q.ok:
        return PhotoCheck(q.short, quality=q)
    return PhotoCheck(None, recognizer.embed(img, q.detection), quality=q)


@dataclass
class PersonResult:
    code: str
    embeddings: list[np.ndarray] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)  # photo file names
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (photo file name, short reason)
    checks: dict[str, QualityResult] = field(default_factory=dict)  # photo file name -> full result


def enroll_folder(folder: Path, detector, recognizer) -> PersonResult:
    """Check and embed every photo in one person's folder."""
    result = PersonResult(folder.name)
    for photo in photo_paths(folder):
        check = check_photo(read_image(photo), detector, recognizer)
        if check.quality is not None:
            result.checks[photo.name] = check.quality
        if check.accepted:
            result.embeddings.append(check.embedding)
            result.accepted.append(photo.name)
        else:
            result.rejected.append((photo.name, check.reason))
    return result


SIDECAR = "photos.json"


KEEP = ("step",)  # per-photo fields that survive a rebuild (set once, e.g. by the guided capture)


def write_sidecar(folder: Path, result: PersonResult, extra: dict[str, dict] | None = None) -> None:
    """known/<code>/photos.json: score, yaw, pitch and the check result of each photo, for the dashboard.

    `extra` adds fields per photo (e.g. {"3.jpg": {"step": "left"}}); fields in KEEP are carried over
    from the previous photos.json for photos that are still there.
    """
    if not folder.is_dir():
        return
    previous = read_sidecar(folder)
    photos = {}
    for name, q in sorted(result.checks.items()):
        entry = q.to_json()
        entry.update({k: v for k, v in previous.get(name, {}).items() if k in KEEP})
        entry.update((extra or {}).get(name, {}))
        photos[name] = entry
    data = {"updated": datetime.now().isoformat(timespec="seconds"),
            "limits": {"max_yaw": config.ENROLL_MAX_YAW, "max_pitch": config.ENROLL_MAX_PITCH},
            "photos": photos}
    atomic_write_bytes(folder / SIDECAR, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def read_sidecar(folder: Path) -> dict[str, dict]:
    """{photo file name: stored check} from photos.json; {} if missing or unreadable."""
    try:
        return json.loads((folder / SIDECAR).read_text(encoding="utf-8"))["photos"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def load_gallery() -> Gallery:
    return Gallery.load() if config.GALLERY_PATH.exists() else Gallery.from_people({})


def enroll_all(detector, recognizer, conn: sqlite3.Connection) -> tuple[Gallery, list[PersonResult], list[str]]:
    """Rebuild the whole gallery from known/ (scripts/enroll.py).

    Returns (gallery, per-person results, removed codes). The gallery is only
    saved if someone is enrolled; people who were in the old gallery but have
    no usable photos now are deactivated.
    """
    folders = sorted(p for p in config.KNOWN_DIR.iterdir() if p.is_dir()) if config.KNOWN_DIR.exists() else []
    results = [enroll_folder(f, detector, recognizer) for f in folders]
    for folder, result in zip(folders, results):
        write_sidecar(folder, result)
    gallery = Gallery.from_people({r.code: r.embeddings for r in results})
    removed: list[str] = []
    if len(gallery):
        with _gallery_lock:
            previous = set(load_gallery().names)
            gallery.save()
            removed = sorted(previous - set(gallery.names))
            db.sync_persons(conn, gallery.names, removed)
    return gallery, results, removed


def enroll_one(code: str, detector, recognizer, conn: sqlite3.Connection,
               extra: dict[str, dict] | None = None) -> PersonResult:
    """Re-embed one person's folder and replace only their gallery entry (the dashboard).

    `extra`: per-photo fields for photos.json, e.g. the guided-capture step.
    """
    result = enroll_folder(person_dir(code), detector, recognizer)
    result.code = code
    write_sidecar(person_dir(code), result, extra)
    with _gallery_lock:
        gallery = load_gallery().replace_person(code, result.embeddings)
        gallery.save()
        db.sync_persons(conn, gallery.names)
    return result


def next_photo_path(code: str) -> Path:
    folder = person_dir(code)
    numbers = [int(p.stem) for p in photo_paths(folder) if p.stem.isdigit()]
    return folder / f"{max(numbers, default=0) + 1}.jpg"


def save_photo(code: str, img: np.ndarray) -> Path:
    """Save as the next numbered JPEG, shrunk to ENROLL_MAX_PHOTO_PX (re-encoding also drops EXIF data)."""
    longest = max(img.shape[:2])
    if longest > config.ENROLL_MAX_PHOTO_PX:
        scale = config.ENROLL_MAX_PHOTO_PX / longest
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    path = next_photo_path(code)
    write_image(path, img)
    return path


def decode_image(data: bytes) -> np.ndarray | None:
    """Decode uploaded bytes (EXIF rotation is applied by OpenCV)."""
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR) if arr.size else None


def add_photos(code: str, images: list[tuple[str, np.ndarray | None]], detector, recognizer,
               conn: sqlite3.Connection) -> tuple[list[tuple[str, PhotoCheck, Path | None]], PersonResult | None]:
    """Check each (label, image); save the accepted ones and rebuild the person's embeddings.

    Returns ([(label, check, saved path or None)], updated person result or None if nothing was saved).
    """
    results = []
    for label, img in images:
        check = check_photo(img, detector, recognizer)
        results.append((label, check, save_photo(code, img) if check.accepted else None))
    person = enroll_one(code, detector, recognizer, conn) if any(p for _, _, p in results) else None
    return results, person


def delete_photo(code: str, filename: str, detector, recognizer, conn: sqlite3.Connection) -> PersonResult:
    """Delete one photo from the person's folder and rebuild their embeddings."""
    folder = person_dir(code)
    matches = [p for p in photo_paths(folder) if p.name == filename]  # only real photo names, no paths
    if not matches:
        raise ValueError(f"no photo {filename!r} for {code}")
    matches[0].unlink()
    return enroll_one(code, detector, recognizer, conn)


def delete_face_data(code: str, reason: str, user: str, conn: sqlite3.Connection, deactivate: bool) -> dict:
    """Remove the person's photos and embeddings; attendance history is kept. Audited."""
    db.require_reason(reason)  # before anything is deleted
    folder = person_dir(code)
    photos = len(photo_paths(folder))
    with _gallery_lock:
        gallery = load_gallery()
        embeddings = gallery.counts().get(code, 0)
        if embeddings:
            gallery.replace_person(code, []).save()
    if folder.exists():
        shutil.rmtree(folder)
    details = {"photos": photos, "embeddings": embeddings}
    db.log_face_data_deleted(conn, code, details, reason, user, deactivate)
    return details
