"""Face detection and the quality filter.

Wraps InsightFace's SCRFD detector from the buffalo_s pack. Each face comes
back with its box, detector score and 5 landmarks (left eye, right eye, nose,
left mouth corner, right mouth corner). Before a face is embedded it must pass
the quality filter: wide enough, sharp enough and roughly facing the camera.
All limits come from ``app.config``.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from app import config


@dataclass
class Detection:
    bbox: np.ndarray  # x1, y1, x2, y2
    kps: np.ndarray  # 5 x 2 landmarks
    score: float

    @property
    def width(self) -> int:
        return int(self.bbox[2] - self.bbox[0])


class Detector:
    def __init__(self, fa=None):
        if fa is None:
            from app.models import face_analysis

            fa = face_analysis()
        self.model = fa.det_model

    def detect(self, img: np.ndarray) -> list[Detection]:
        """Detect faces in a BGR image, largest first."""
        bboxes, kpss = self.model.detect(img)
        dets = [Detection(bbox=b[:4], kps=k, score=float(b[4])) for b, k in zip(bboxes, kpss)]
        return sorted(dets, key=lambda d: d.width, reverse=True)


def resize_to_width(img: np.ndarray, width: int = config.FRAME_WIDTH) -> np.ndarray:
    """Shrink a frame to ``width`` pixels wide; never enlarges."""
    if img.shape[1] <= width:
        return img
    scale = width / img.shape[1]
    return cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def blur_score(img: np.ndarray, bbox: np.ndarray) -> float | None:
    """Variance of Laplacian of the face crop resized to 112x112; None if the crop is empty."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox.astype(int)
    x1, y1, x2, y2 = max(x1, 0), max(y1, 0), min(x2, w), min(y2, h)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    crop = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    crop = cv2.resize(crop, (112, 112), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(crop, cv2.CV_64F).var())


def pose_ratios(kps: np.ndarray) -> tuple[float, float]:
    """Rough head pose from 5 landmarks.

    yaw: horizontal nose offset from the eye midpoint divided by eye distance
    (0 when facing straight on). pitch: where the nose sits between the eye
    line and the mouth line (about 0.5 when level).
    """
    left_eye, right_eye, nose, left_mouth, right_mouth = kps
    eye_mid = (left_eye + right_eye) / 2
    mouth_mid = (left_mouth + right_mouth) / 2
    eye_dist = max(float(np.linalg.norm(right_eye - left_eye)), 1e-6)
    yaw = abs(float(nose[0] - eye_mid[0])) / eye_dist
    face_h = max(float(mouth_mid[1] - eye_mid[1]), 1e-6)
    pitch = float(nose[1] - eye_mid[1]) / face_h
    return yaw, pitch


def quality_score(det: Detection) -> float:
    """Higher is better: bigger, more confident, more frontal faces. Used to pick a track's best frames."""
    yaw, _ = pose_ratios(det.kps)
    return det.width * det.score * (1.0 - min(yaw, 1.0))


def check_quality(img: np.ndarray, det: Detection) -> str | None:
    """Return why the face fails the quality filter, or None if it passes."""
    if det.width < config.MIN_FACE_PX:
        return f"too small ({det.width}px < {config.MIN_FACE_PX})"
    blur = blur_score(img, det.bbox)
    if blur is None:
        return "out of frame"
    if blur < config.MIN_BLUR_VAR:
        return f"blurry (var {blur:.0f} < {config.MIN_BLUR_VAR:.0f})"
    yaw, pitch = pose_ratios(det.kps)
    if yaw > config.MAX_YAW_RATIO:
        return f"turned sideways (yaw {yaw:.2f} > {config.MAX_YAW_RATIO})"
    low, high = config.PITCH_RANGE
    if not low <= pitch <= high:
        return f"looking up/down (pitch {pitch:.2f} not in {low}-{high})"
    return None
