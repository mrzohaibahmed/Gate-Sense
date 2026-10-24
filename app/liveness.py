"""Liveness (anti-spoofing): is this a real face, or a phone screen or printed photo held up to the camera?

Silent-Face-Anti-Spoofing by MiniVision (https://github.com/minivision-ai/Silent-Face-Anti-Spoofing, Apache-2.0):
two MiniFASNet models, each on an 80x80 crop of the frame around the face, 2.7 and 4 times the face box, because
the edge of a phone or a sheet of paper around the face is what gives a spoof away. That is why the crop comes from
the full-resolution camera frame, never from a tight face crop. The input is the raw BGR crop as float 0-255
(no normalisation), NCHW; each model returns 3 logits and class 1 is "real". As in the upstream test.py, the live
score is the average of the two models' softmax "real" probabilities (0-1).

The ONNX files are yakhyo/face-anti-spoofing's exports of the official weights (Apache-2.0). They are downloaded
to LIVENESS_MODEL_DIR on first use or by scripts/get_models.py, and checked against the SHA-256 in
config.LIVENESS_MODELS: a file that doesn't match is never loaded.
"""

import hashlib
import os
import time
import urllib.request
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import cv2
import numpy as np

from app import config

SOURCE = "Silent-Face-Anti-Spoofing (MiniVision, Apache-2.0); ONNX export: yakhyo/face-anti-spoofing (Apache-2.0)"
REAL = 1  # the models' "real face" class


class LivenessUnavailable(RuntimeError):
    """The models are missing and couldn't be downloaded, or a file doesn't match its SHA-256."""


@dataclass
class LivenessResult:
    score: float  # 0-1: the models' average "real face" probability
    live: bool  # score >= the threshold
    ms: float  # time the check took


def crop_box(src_w: int, src_h: int, box: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int]:
    """Upstream's CropImage._get_new_box: the face box (x, y, w, h) grown `scale` times around its centre (less if
    the frame is too small), then shifted, not cut, to stay inside the frame. (x1, y1, x2, y2), inclusive."""
    x, y, w, h = box
    scale = min((src_h - 1) / h, (src_w - 1) / w, scale)
    new_w, new_h = w * scale, h * scale
    cx, cy = x + w / 2, y + h / 2
    x1, y1, x2, y2 = cx - new_w / 2, cy - new_h / 2, cx + new_w / 2, cy + new_h / 2
    if x1 < 0:
        x2 -= x1
        x1 = 0
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if x2 > src_w - 1:
        x1 -= x2 - src_w + 1
        x2 = src_w - 1
    if y2 > src_h - 1:
        y1 -= y2 - src_h + 1
        y2 = src_h - 1
    return int(x1), int(y1), int(x2), int(y2)


def face_box(bbox) -> tuple[int, int, int, int]:
    """A detector box (x1, y1, x2, y2) as upstream's (x, y, w, h)."""
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    return int(x1), int(y1), max(1, int(x2 - x1 + 1)), max(1, int(y2 - y1 + 1))


def crop(img: np.ndarray, bbox, scale: float, size: int = 80) -> np.ndarray:
    """The face and `scale` times its box around it, resized to size x size (the models' input)."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = crop_box(w, h, face_box(bbox), scale)
    return cv2.resize(img[y1:y2 + 1, x1:x2 + 1], (size, size))


def context_crop(img: np.ndarray, bbox, scale: float = 2.7, size: int | None = None) -> np.ndarray:
    """What the first model looked at, at up to `size` px (SPOOF_SNAPSHOT_SIZE): the review snapshot of a spoof, so
    the phone's edge or the paper shows."""
    size = size or config.SPOOF_SNAPSHOT_SIZE
    h, w = img.shape[:2]
    x1, y1, x2, y2 = crop_box(w, h, face_box(bbox), scale)
    out = img[y1:y2 + 1, x1:x2 + 1]
    k = size / max(out.shape[:2])
    return cv2.resize(out, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else out.copy()


def softmax(logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - logits.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


class Liveness:
    """The two models. `models`: [(onnxruntime session, crop scale)], injectable for tests."""

    def __init__(self, models: list | None = None):
        if models is None:
            models = [(session(path), scale) for path, scale in model_files()]
        self.models = [(s, scale, s.get_inputs()[0].name, input_size(s)) for s, scale in models]

    def score(self, img: np.ndarray, bbox) -> float:
        """The average "real face" probability of the models (0-1) for the face at `bbox` in `img`."""
        total = 0.0
        for s, scale, name, size in self.models:
            x = crop(img, bbox, scale, size).astype(np.float32).transpose(2, 0, 1)[None]
            total += float(softmax(s.run(None, {name: x})[0])[0, REAL])
        return total / len(self.models)

    def check(self, img: np.ndarray, bbox, threshold: float | None = None) -> LivenessResult:
        threshold = config.LIVENESS_THRESHOLD if threshold is None else threshold
        t0 = time.perf_counter()
        score = self.score(img, bbox)
        return LivenessResult(round(score, 3), score >= threshold, (time.perf_counter() - t0) * 1000)


def input_size(s) -> int:
    shape = s.get_inputs()[0].shape
    return shape[2] if isinstance(shape[2], int) else 80


def session(path: Path):
    """LIVENESS_THREADS threads, and no spin-waiting after a run: the models are tiny, and threads left spinning
    take the CPU from the detector and recognizer that run next (measured on a 2-core i5: 156 ms a frame with
    onnxruntime's defaults, 93 ms like this, 72 ms without liveness)."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = config.LIVENESS_THREADS
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return ort.InferenceSession(str(path), options, providers=config.PROVIDERS)


# --- Model files: download and verify ------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def problems(folder: Path | None = None) -> dict[str, str]:
    """{file: "missing" or "SHA-256 mismatch"} for each model that can't be used as it is."""
    folder = folder or config.LIVENESS_MODEL_DIR
    found = {}
    for name, (_url, digest, _scale) in config.LIVENESS_MODELS.items():
        path = folder / name
        if not path.is_file():
            found[name] = "missing"
        elif sha256(path) != digest:
            found[name] = "SHA-256 mismatch"
    return found


def fetch(url: str, dest: Path) -> None:
    """Download `url` to `dest` (replaced). urlopen is injectable through this function in tests."""
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)


def download(folder: Path | None = None, log=print) -> list[Path]:
    """Download every model that is missing or doesn't match its SHA-256; each file is written as .partial,
    checked, then renamed. LivenessUnavailable if a download fails or a downloaded file doesn't match."""
    folder = folder or config.LIVENESS_MODEL_DIR
    bad = problems(folder)
    for name in bad:
        url, digest, _scale = config.LIVENESS_MODELS[name]
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / f"{name}.partial"
        log(f"Downloading liveness model {name} from {url}")
        try:
            fetch(url, part)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise LivenessUnavailable(f"couldn't download {name} ({e}); run scripts\\get_models.py on a PC with "
                                      f"internet access, or copy the files to {folder}") from None
        got = sha256(part)
        if got != digest:
            part.unlink(missing_ok=True)
            raise LivenessUnavailable(f"{name} downloaded from {url} doesn't match its SHA-256 (got {got}, "
                                      f"expected {digest}); not used")
        os.replace(part, folder / name)
    return [folder / name for name in config.LIVENESS_MODELS]


def model_files(folder: Path | None = None) -> list[tuple[Path, float]]:
    """[(path, crop scale)] of verified model files, downloading any that are missing (like InsightFace does)."""
    folder = folder or config.LIVENESS_MODEL_DIR
    if problems(folder):
        download(folder)
    return [(folder / name, scale) for name, (_url, _digest, scale) in config.LIVENESS_MODELS.items()]


@cache
def shared() -> Liveness:
    """One Liveness per process (the API's enrollment wizard); LivenessUnavailable if the models can't be had."""
    return Liveness()
