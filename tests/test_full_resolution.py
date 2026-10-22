"""Detection on the FRAME_WIDTH copy, everything after it on the full-resolution frame.

Box/landmark scaling, the quality filter measured in camera pixels, and ArcFace alignment from the
full frame. The last test uses the real buffalo_s models and InsightFace's sample photo; it is skipped
when they are not on disk.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
from insightface.utils import face_align

from app import config
from app.detector import (
    Detection, blur_score, check_quality, detection_scale, pose_ratios, resize_to_width, scale_detection,
)
from tests.fakes import FRONTAL_112


def face(x: float, y: float, size: float, score: float = 0.9) -> Detection:
    return Detection(np.array([x, y, x + size, y + size], np.float32), FRONTAL_112 * (size / 112) + [x, y], score)


def noise(h: int, w: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


# --- Scaling ------------------------------------------------------------------------


def test_scale_detection_moves_box_and_landmarks():
    det = face(200, 100, 50, score=0.77)
    full = scale_detection(det, 3.0)
    assert np.allclose(full.bbox, [600, 300, 750, 450])
    assert np.allclose(full.kps, det.kps * 3) and full.kps.shape == (5, 2)
    assert full.score == 0.77 and full.width == 150
    assert full.bbox.dtype == det.bbox.dtype and full.kps.dtype == det.kps.dtype
    assert np.allclose(det.bbox, [200, 100, 250, 150])  # the detection-frame face is left as it was


def test_scale_one_is_the_same_detection():
    det = face(10, 10, 100)
    assert scale_detection(det, detection_scale(np.zeros((480, 640, 3)), np.zeros((480, 640, 3)))) is det


@pytest.mark.parametrize("w, h", [(1920, 1080), (1280, 720), (2560, 1440), (704, 576), (640, 480)])
def test_scaled_points_land_on_the_same_spot(w, h):
    """A spot found in the resized frame, scaled back, is where that spot is in the camera frame."""
    frame = np.zeros((h, w, 3), np.uint8)
    x0, y0 = int(w * 0.6), int(h * 0.3)
    frame[y0:y0 + 30, x0:x0 + 30] = 255
    small = resize_to_width(frame)
    scale = detection_scale(frame, small)
    assert small.shape[1] == min(w, config.FRAME_WIDTH)

    def centre(img):
        ys, xs = np.nonzero(img[:, :, 0])
        weights = img[ys, xs, 0].astype(float)
        return np.array([np.average(xs, weights=weights), np.average(ys, weights=weights)]) + 0.5  # pixel centres

    found = Detection(np.zeros(4, np.float32), np.tile(centre(small), (5, 1)).astype(np.float32), 0.9)
    assert np.allclose(scale_detection(found, scale).kps[0], centre(frame), atol=0.5)


# --- Quality filter in camera pixels -------------------------------------------------


def test_min_face_px_is_camera_pixels():
    frame = noise(1080, 1920)
    small = resize_to_width(frame)
    scale = detection_scale(frame, small)
    assert scale == 3.0
    det = face(200, 100, 50)  # 50 px in the 640 px detection frame = 150 camera px
    assert check_quality(small, det) == f"too small (50px < {config.MIN_FACE_PX})"  # what the old path did
    assert check_quality(frame, scale_detection(det, scale)) is None
    tiny = face(200, 100, 20)  # 60 camera px: still too small, and the message says camera pixels
    assert check_quality(frame, scale_detection(tiny, scale)) == f"too small (60px < {config.MIN_FACE_PX})"


def test_blur_is_measured_on_the_full_frame():
    """Detail the 640 px copy averages away (fine diagonal lines) still counts as sharp at full resolution."""
    frame = np.zeros((1080, 1920, 3), np.uint8)
    frame[(np.indices((1080, 1920)).sum(axis=0) % 3) == 0] = 255  # every 3x3 block averages to the same grey
    small = resize_to_width(frame)
    det = face(200, 100, 50)
    full = scale_detection(det, detection_scale(frame, small))
    assert blur_score(small, det.bbox) < config.MIN_BLUR_VAR  # the old path would have called it blurry
    assert blur_score(frame, full.bbox) > config.MIN_BLUR_VAR
    assert check_quality(frame, full) is None


def test_pose_is_the_same_at_any_scale():
    kps = FRONTAL_112.copy()
    kps[2, 0] += 20  # nose toward one eye: turned sideways
    det = Detection(np.array([0, 0, 112, 112], np.float32) + 200, kps + 200, 0.9)
    full = scale_detection(det, 3.0)
    assert np.allclose(pose_ratios(full.kps), pose_ratios(det.kps))
    assert check_quality(noise(1080, 1920), full).startswith("turned sideways")


# --- ArcFace alignment from the full frame --------------------------------------------


def test_alignment_from_full_frame_matches_the_detection_frame():
    """norm_crop with the scaled landmarks on the full frame shows the same face as on the 640 px copy."""
    frame = cv2.GaussianBlur(noise(1080, 1920, seed=3), (0, 0), 6)
    frame = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX)  # smooth, high-contrast pattern
    small = resize_to_width(frame)
    det = face(250, 80, 150)
    full = scale_detection(det, detection_scale(frame, small))
    a = face_align.norm_crop(small, landmark=det.kps, image_size=112).astype(float)
    b = face_align.norm_crop(frame, landmark=full.kps, image_size=112).astype(float)
    assert np.abs(a - b).mean() < 6  # out of 255: same content, same place (minus the detail the copy lost)
    other = face_align.norm_crop(frame, landmark=det.kps, image_size=112).astype(float)  # unscaled: wrong spot
    assert np.abs(a - other).mean() > 20


SAMPLE = Path(face_align.__file__).resolve().parent.parent / "data" / "images" / "t1.jpg"
MODEL_DIR = Path.home() / ".insightface" / "models" / config.MODEL_PACK


@pytest.mark.skipif(not (SAMPLE.is_file() and (MODEL_DIR / "w600k_mbf.onnx").is_file()),
                    reason="needs the buffalo_s models and InsightFace's sample photo")
def test_real_embeddings_full_resolution_vs_old_path():
    from app.detector import Detector
    from app.recognizer import Recognizer

    detector, recognizer = Detector(), Recognizer()
    photo = cv2.imread(str(SAMPLE))  # 1280x886, six faces about 100 px wide

    # A 640 px input (a webcam): nothing is resized, so the new path is exactly the old one.
    webcam = np.ascontiguousarray(photo[150:630, 40:680])
    small = resize_to_width(webcam)
    assert small is webcam and detection_scale(webcam, small) == 1
    dets = detector.detect(small)
    assert dets
    for d in dets:
        assert np.array_equal(recognizer.embed(webcam, scale_detection(d, 1.0)), recognizer.embed(small, d))

    # A 1920 px camera frame: the full-resolution embedding matches the old 640 px one, and is as good as
    # detecting at full resolution directly.
    frame = cv2.resize(photo, (1920, 1329), interpolation=cv2.INTER_CUBIC)
    small = resize_to_width(frame)
    scale = detection_scale(frame, small)
    direct = detector.detect(frame)
    dets = detector.detect(small)
    assert len(dets) == 6
    for d in dets:
        full = scale_detection(d, scale)
        old = recognizer.embed(small, d)
        new = recognizer.embed(frame, full)
        ref = recognizer.embed(frame, min(direct, key=lambda x: np.abs(x.bbox - full.bbox).sum()))
        assert float(old @ new) > 0.9  # the same person either way
        assert float(new @ ref) > 0.99  # scaled landmarks align as well as full-resolution detection
