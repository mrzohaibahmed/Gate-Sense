"""Every enrollment photo-quality rule, in one place: check_photo(image) -> QualityResult.

Used by the dashboard's Enrollment page and scripts/enroll.py (through app.enrollment). Each refusal
has a plain message for the person enrolling (`reason`) and a short technical one (`short`, for the
command line and logs). The live cameras use their own, faster filter (app.detector.check_quality).

Rules, in order: readable image -> a face found (detector confidence >= ENROLL_DET_THRESHOLD; a dark
image without a face says "Too dark") -> exactly one face -> face at least MIN_FACE_PX wide -> inside
the photo -> bright enough (ENROLL_MIN_BRIGHTNESS) -> turned at most ENROLL_MAX_YAW degrees -> tilted at
most ENROLL_MAX_PITCH degrees -> sharp enough (MIN_BLUR_VAR). Pose is checked before blur so a turned
face gets "turn toward the camera" rather than "blurry".

Head pose (yaw, pitch in degrees) comes from InsightFace's 3D-landmark model when the detector offers it
(Detector.pose); otherwise a rough estimate from the 5 landmarks is used and marked approximate.
"""

import math
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from app import config
from app.detector import Detection, blur_score, pose_ratios


@dataclass
class QualityResult:
    ok: bool
    reason: str  # plain message for the person enrolling; "" when ok
    score: float  # 0-100: how good the photo is for recognition (also computed for refused photos)
    yaw: float | None  # degrees turned left/right (0 = facing the camera); None without a face
    pitch: float | None  # degrees tilted up/down
    face_px: int  # face box width in pixels
    blur: float  # sharpness (variance of Laplacian on the face at 112x112)
    det_score: float  # detector confidence
    brightness: float = 0.0  # mean face brightness 0-255
    code: str = "ok"  # unreadable / no_face / too_dark / faces / too_small / out_of_frame / turned / tilted / blurry
    short: str = ""  # short technical reason, e.g. "turned sideways (52° > 35°)"
    pose_source: str = ""  # "3D landmarks" or "5 landmarks (approximate)"
    detection: Detection | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("detection")
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()}


def landmark_pose(det: Detection) -> tuple[float, float, str]:
    """Rough yaw/pitch from the 5 landmarks (used only when the 3D-landmark model isn't available)."""
    left_eye, right_eye, nose = det.kps[0], det.kps[1], det.kps[2]
    yaw_ratio, pitch_ratio = pose_ratios(det.kps)
    side = 1.0 if nose[0] >= (left_eye[0] + right_eye[0]) / 2 else -1.0
    yaw = side * math.degrees(math.atan(yaw_ratio / 0.5))  # nose ~0.5 eye-distances in front of the eyes
    pitch = math.degrees(math.atan((pitch_ratio - 0.5) / 0.5))
    return yaw, pitch, "5 landmarks (approximate)"


def face_brightness(img: np.ndarray, bbox: np.ndarray) -> float:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox.astype(int)
    crop = img[max(y1, 0):min(y2, h), max(x1, 0):min(x2, w)]
    if crop.size == 0:
        return 0.0
    return float(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).mean())


def quality_score(face_px: int, blur: float, yaw: float, pitch: float, det_score: float, brightness: float) -> float:
    """0-100 ranking of how useful a photo is: frontal, sharp, large, confident, well lit."""
    def clip(v: float) -> float:
        return max(0.0, min(1.0, v))

    pose = clip(1 - max(abs(yaw) / config.ENROLL_MAX_YAW, abs(pitch) / config.ENROLL_MAX_PITCH) * 0.8)
    sharp = clip(blur / (3 * config.MIN_BLUR_VAR))
    size = clip(face_px / 200)
    conf = clip((det_score - 0.3) / 0.5)
    light = clip(min((brightness - 20) / 60, (250 - brightness) / 60))
    return round(100 * (0.3 * pose + 0.2 * sharp + 0.2 * size + 0.15 * conf + 0.15 * light), 1)


def _refuse(code: str, reason: str, short: str, **values) -> QualityResult:
    base = dict(score=0.0, yaw=None, pitch=None, face_px=0, blur=0.0, det_score=0.0)
    base.update(values)
    return QualityResult(False, reason, code=code, short=short, **base)


def check_photo(image: np.ndarray | None, detector=None) -> QualityResult:
    """Run every enrollment rule on one photo. `detector` defaults to the shared SCRFD detector."""
    if detector is None:
        from app.detector import Detector

        detector = Detector()
    if image is None or getattr(image, "size", 0) == 0:
        return _refuse("unreadable", "Couldn't read this image. Use a JPG or PNG photo.", "unreadable image")

    dets = detector.detect(image, det_thresh=config.ENROLL_DET_THRESHOLD)
    if not dets:
        mean = float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean())
        if mean < config.ENROLL_MIN_BRIGHTNESS:
            return _refuse("too_dark", "Too dark: no face could be seen. Add light in front of the person.",
                           f"too dark (no face, brightness {mean:.0f})", brightness=mean)
        return _refuse("no_face", "No face found. Move closer and face the camera.", "no face", brightness=mean)
    if len(dets) > 1:
        count = "Two faces" if len(dets) == 2 else f"{len(dets)} faces"
        return _refuse("faces", f"{count} in the photo. Only the person enrolling should be visible.",
                       f"{len(dets)} faces")

    det = dets[0]
    blur = blur_score(image, det.bbox)
    bright = face_brightness(image, det.bbox)
    pose = getattr(detector, "pose", None)
    yaw, pitch, source = (pose(image, det) if pose else None) or landmark_pose(det)
    values = dict(yaw=round(yaw, 1), pitch=round(pitch, 1), face_px=det.width, blur=round(blur or 0.0, 1),
                  det_score=round(det.score, 3), brightness=round(bright, 1), pose_source=source)
    values["score"] = quality_score(det.width, blur or 0.0, yaw, pitch, det.score, bright)

    def refuse(code: str, reason: str, short: str) -> QualityResult:
        return QualityResult(False, reason, code=code, short=short, detection=det, **values)

    max_yaw, max_pitch = config.ENROLL_MAX_YAW, config.ENROLL_MAX_PITCH
    if det.width < config.MIN_FACE_PX:
        return refuse("too_small", f"Face too small ({det.width} px). Move closer.",
                      f"too small ({det.width}px < {config.MIN_FACE_PX})")
    if blur is None:
        return refuse("out_of_frame", "The face is cut off at the edge of the photo. Move to the middle.",
                      "out of frame")
    if bright < config.ENROLL_MIN_BRIGHTNESS:
        return refuse("too_dark", f"Too dark (brightness {bright:.0f} of 255). Add light in front of the person.",
                      f"too dark ({bright:.0f} < {config.ENROLL_MIN_BRIGHTNESS})")
    if abs(yaw) > max_yaw:
        return refuse("turned", f"Face turned too far to the side ({abs(yaw):.0f}°). "
                                f"Turn a little toward the camera (max {max_yaw:.0f}°).",
                      f"turned sideways ({abs(yaw):.0f}° > {max_yaw:.0f}°)")
    if abs(pitch) > max_pitch:
        return refuse("tilted", f"Head tilted too far up or down ({abs(pitch):.0f}°). "
                                f"Look straight at the camera (max {max_pitch:.0f}°).",
                      f"looking up/down ({abs(pitch):.0f}° > {max_pitch:.0f}°)")
    if blur < config.MIN_BLUR_VAR:
        return refuse("blurry", "Photo is blurry. Hold still and check that the camera is in focus.",
                      f"blurry (var {blur:.0f} < {config.MIN_BLUR_VAR:.0f})")
    return QualityResult(True, "", code="ok", short="", detection=det, **values)
