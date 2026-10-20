"""Image file helpers and face snapshots."""

from pathlib import Path

import cv2
import numpy as np


def read_image(path: Path) -> np.ndarray | None:
    # imdecode + fromfile handles non-ASCII Windows paths, which cv2.imread does not.
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def write_image(path: Path, img: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(path.suffix, img)
    if not ok:
        raise RuntimeError(f"could not encode {path}")
    buf.tofile(str(path))


def crop_face(img: np.ndarray, bbox: np.ndarray, size: int, pad: float = 0.2) -> np.ndarray:
    """Face crop with `pad` margin on each side, shrunk so its longest side is at most `size` px."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    px, py = (x2 - x1) * pad, (y2 - y1) * pad
    x1, y1 = max(int(x1 - px), 0), max(int(y1 - py), 0)
    x2, y2 = min(int(x2 + px), w), min(int(y2 + py), h)
    crop = img[y1:y2, x1:x2].copy()
    longest = max(crop.shape[:2]) if crop.size else 0
    if longest > size:
        scale = size / longest
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return crop
