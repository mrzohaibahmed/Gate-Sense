"""Fake detector/recognizer and synthetic photos for enrollment tests (no models needed).

A photo's 20x20 top-left block encodes what the fake detector "sees", so the
result survives saving as JPEG: ~30 no face, ~100 one face, ~170 two faces,
~240 one small face. The rest of the image is noise (sharp) or blurred noise.
"""

import cv2
import numpy as np

from app.detector import Detection

FRONTAL_112 = np.array([[38.3, 51.7], [73.5, 51.5], [56.0, 71.7], [41.5, 92.4], [70.7, 92.2]], np.float32)
MARKERS = {"none": 30, "one": 100, "two": 170, "small": 240}


def face(x: float, y: float, size: float) -> Detection:
    return Detection(np.array([x, y, x + size, y + size], np.float32), FRONTAL_112 * (size / 112) + [x, y], 0.9)


def photo(kind: str = "good", seed: int = 0) -> np.ndarray:
    """kind: good, none, two, small or blurry."""
    img = np.random.default_rng(seed).integers(0, 256, (320, 320, 3), dtype=np.uint8)
    if kind == "blurry":
        img = cv2.GaussianBlur(img, (31, 31), 12)
    img[0:20, 0:20] = MARKERS.get(kind, MARKERS["one"])
    return img


class FakeDetector:
    def detect(self, img):
        marker = float(img[5:15, 5:15].mean())
        if marker < 65:
            return []
        if marker < 135:
            return [face(60, 60, 200)]
        if marker < 205:
            return [face(30, 30, 120), face(170, 170, 120)]
        return [face(100, 100, 50)]


class FakeRecognizer:
    def embed(self, img, det):
        v = np.random.default_rng(int(img.sum()) % (2**32)).standard_normal(512).astype(np.float32)
        return v / np.linalg.norm(v)
