import cv2
import numpy as np

from app import config
from app.detector import Detection, blur_score, check_quality, pose_ratios, resize_to_width

# ArcFace's reference 5-point layout for a 112x112 frontal face.
FRONTAL_KPS = np.array(
    [[38.29, 51.70], [73.53, 51.50], [56.03, 71.74], [41.55, 92.37], [70.73, 92.20]],
    dtype=np.float32,
)


def face_at(scale: float, kps=FRONTAL_KPS) -> Detection:
    """A detection covering a 112x112 face scaled by `scale`."""
    return Detection(bbox=np.array([0, 0, 112, 112], np.float32) * scale, kps=kps * scale, score=0.9)


def sharp_image(size: int) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(0, 256, (size, size, 3), dtype=np.uint8)


def test_frontal_pose_ratios():
    yaw, pitch = pose_ratios(FRONTAL_KPS)
    assert yaw < 0.05
    assert 0.45 < pitch < 0.55


def test_frontal_sharp_face_passes():
    assert check_quality(sharp_image(224), face_at(2.0)) is None


def test_small_face_rejected():
    assert check_quality(sharp_image(224), face_at(0.5)).startswith("too small")


def test_blurry_face_rejected():
    img = cv2.GaussianBlur(sharp_image(224), (31, 31), 10)
    assert blur_score(img, face_at(2.0).bbox) < config.MIN_BLUR_VAR
    assert check_quality(img, face_at(2.0)).startswith("blurry")


def test_turned_face_rejected():
    kps = FRONTAL_KPS.copy()
    kps[2, 0] += 20  # nose far toward one eye
    assert check_quality(sharp_image(224), face_at(2.0, kps)).startswith("turned sideways")


def test_face_out_of_frame():
    det = Detection(bbox=np.array([300, 300, 400, 400], np.float32), kps=FRONTAL_KPS + 300, score=0.9)
    assert check_quality(sharp_image(224), det) == "out of frame"


def test_resize_only_shrinks():
    assert resize_to_width(np.zeros((480, 1280, 3), np.uint8), 640).shape == (240, 640, 3)
    assert resize_to_width(np.zeros((240, 320, 3), np.uint8), 640).shape == (240, 320, 3)
