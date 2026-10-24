"""Liveness (app/liveness.py): upstream's crop, the input the models get, the averaged live score, and the model files
(download, SHA-256 check). Fake onnxruntime sessions: no models, no network."""

import hashlib

import numpy as np
import pytest

from app import config, liveness
from app.liveness import Liveness, LivenessUnavailable, crop, crop_box


class Input:
    name, shape = "input", ["batch_size", 3, 80, 80]


class FakeSession:
    """An onnxruntime session that returns fixed logits and keeps what it was given."""

    def __init__(self, logits):
        self.logits = np.array([logits], np.float32)
        self.seen = []

    def get_inputs(self):
        return [Input()]

    def run(self, outputs, feeds):
        self.seen.append(feeds["input"])
        return [self.logits]


# --- Crop (upstream CropImage._get_new_box) --------------------------------------------------------------------


def test_crop_box_grows_the_face_around_its_centre():
    # A 100x100 face at (400, 300) in a 1920x1080 frame, 2.7 times: 270x270 around the same centre.
    assert crop_box(1920, 1080, (400, 300, 100, 100), 2.7) == (315, 215, 585, 485)


def test_crop_box_is_shifted_inside_the_frame_not_cut():
    x1, y1, x2, y2 = crop_box(1920, 1080, (10, 20, 100, 100), 4.0)  # near the top-left corner
    assert (x1, y1) == (0, 0) and (x2, y2) == (400, 400)  # still 4 x 100 wide and high
    x1, y1, x2, y2 = crop_box(640, 480, (560, 400, 60, 60), 2.7)  # near the bottom-right corner
    assert (x2, y2) == (639, 479) and (x2 - x1, y2 - y1) == (162, 162)


def test_crop_box_scale_shrinks_when_the_frame_is_too_small():
    # 4 x 200 = 800 doesn't fit a 640x480 frame: the scale drops to (480 - 1) / 200, as upstream.
    x1, y1, x2, y2 = crop_box(640, 480, (220, 140, 200, 200), 4.0)
    assert (y1, y2) == (0, 479) and x2 - x1 == 479


def test_crop_is_the_raw_bgr_region_at_80px():
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[:, :960] = (255, 0, 0)  # left half blue (BGR)
    out = crop(img, np.array([400, 300, 500, 400], np.float32), 2.7)
    assert out.shape == (80, 80, 3) and (out == (255, 0, 0)).all()


def test_context_crop_is_what_the_model_saw_at_review_size():
    img = np.random.default_rng(0).integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
    snap = liveness.context_crop(img, np.array([800, 400, 1000, 600], np.float32))
    assert snap.shape == (config.SPOOF_SNAPSHOT_SIZE, config.SPOOF_SNAPSHOT_SIZE, 3)  # 540 px crop, shrunk


# --- Score ----------------------------------------------------------------------------------------------------


def test_score_averages_the_models_real_probability_on_raw_pixels():
    a, b = FakeSession([0.0, 2.0, 0.0]), FakeSession([1.0, 0.0, 1.0])  # class 1 = real
    model = Liveness([(a, 2.7), (b, 4.0)])
    img = np.full((480, 640, 3), (10, 20, 30), np.uint8)
    score = model.score(img, np.array([270, 190, 370, 290], np.float32))
    pa, pb = np.exp(2) / (np.exp(2) + 2), 1 / (2 * np.e + 1)
    assert score == pytest.approx((pa + pb) / 2)
    x = a.seen[0]
    assert x.shape == (1, 3, 80, 80) and x.dtype == np.float32
    assert (x[0, 0] == 10).all() and (x[0, 2] == 30).all()  # BGR order, 0-255, not normalised


def test_check_uses_the_configured_threshold(monkeypatch):
    model = Liveness([(FakeSession([0.0, 1.0, 0.0]), 2.7)])  # score ~0.58
    box = np.array([270, 190, 370, 290], np.float32)
    img = np.zeros((480, 640, 3), np.uint8)
    result = model.check(img, box)
    assert result.score == pytest.approx(0.576, abs=1e-3) and result.live is False and result.ms >= 0
    monkeypatch.setattr(config, "LIVENESS_THRESHOLD", 0.5)
    assert model.check(img, box).live is True
    assert model.check(img, box, threshold=0.9).live is False


# --- Model files ----------------------------------------------------------------------------------------------


GOOD = b"onnx model bytes"


@pytest.fixture
def models(monkeypatch):
    """One model whose SHA-256 is that of GOOD; `served` is what the fake download returns."""
    digest = hashlib.sha256(GOOD).hexdigest()
    monkeypatch.setattr(config, "LIVENESS_MODELS", {"m.onnx": ("https://example.test/m.onnx", digest, 2.7)})
    served = {"data": GOOD, "urls": []}

    def fetch(url, dest):
        served["urls"].append(url)
        if served["data"] is None:
            raise OSError("network unreachable")
        dest.write_bytes(served["data"])

    monkeypatch.setattr(liveness, "fetch", fetch)
    return served


def test_download_verifies_and_keeps_a_matching_file(models):
    folder = config.LIVENESS_MODEL_DIR
    assert liveness.problems() == {"m.onnx": "missing"}
    assert liveness.model_files() == [(folder / "m.onnx", 2.7)]  # downloaded on first use
    assert (folder / "m.onnx").read_bytes() == GOOD and not list(folder.glob("*.partial"))
    assert liveness.problems() == {}
    liveness.model_files()
    assert models["urls"] == ["https://example.test/m.onnx"]  # verified files aren't downloaded again


def test_a_file_that_doesnt_match_is_never_kept(models):
    models["data"] = b"something else"
    with pytest.raises(LivenessUnavailable, match="doesn't match its SHA-256"):
        liveness.download()
    assert not any(config.LIVENESS_MODEL_DIR.iterdir())


def test_a_damaged_file_is_downloaded_again(models):
    config.LIVENESS_MODEL_DIR.mkdir(parents=True)
    (config.LIVENESS_MODEL_DIR / "m.onnx").write_bytes(b"truncated")
    assert liveness.problems() == {"m.onnx": "SHA-256 mismatch"}
    liveness.download()
    assert (config.LIVENESS_MODEL_DIR / "m.onnx").read_bytes() == GOOD


def test_no_network_says_what_to_do(models):
    models["data"] = None
    with pytest.raises(LivenessUnavailable, match="get_models.py"):
        liveness.model_files()
    assert not list(config.LIVENESS_MODEL_DIR.glob("*"))


def test_tests_never_download_the_real_models():
    """conftest points LIVENESS_MODEL_DIR at a temporary folder and blocks downloads."""
    with pytest.raises(LivenessUnavailable, match="no downloads in tests"):
        liveness.shared()


def test_the_real_models_are_pinned():
    assert set(config.LIVENESS_MODELS) == {"MiniFASNetV2.onnx", "MiniFASNetV1SE.onnx"}
    assert [m[2] for m in config.LIVENESS_MODELS.values()] == [2.7, 4.0]
    assert all(m[0].startswith("https://") and len(m[1]) == 64 for m in config.LIVENESS_MODELS.values())


# --- Settings -------------------------------------------------------------------------------------------------


def test_liveness_settings_are_editable():
    assert config.check_setting("LIVENESS_ENABLED", False) is False
    assert config.check_setting("LIVENESS_ENABLED", 1) is True
    with pytest.raises(ValueError, match="on or off"):
        config.check_setting("LIVENESS_ENABLED", 2)
    assert config.check_setting("LIVENESS_THRESHOLD", 0.7) == 0.7
    with pytest.raises(ValueError):
        config.check_setting("LIVENESS_THRESHOLD", 1.5)
    assert config.check_setting("LIVENESS_MIN_FRAMES", 4) == 4
    assert (config.LIVENESS_ENABLED, config.LIVENESS_THRESHOLD, config.LIVENESS_MIN_FRAMES) == (True, 0.7, 3)
