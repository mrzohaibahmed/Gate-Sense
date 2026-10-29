"""Environment check.

Imports every dependency, loads the InsightFace buffalo_s model on the device GATESENSE_DEVICE picks
(downloading it on first run), grabs one webcam frame and reports how many
faces were found and how long detection took.

Run from the project root:
    .venv\\Scripts\\python scripts\\check_env.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402


def main() -> int:
    print(f"Python      {sys.version.split()[0]}")

    import numpy as np
    import cv2
    import onnxruntime as ort
    import insightface

    from app.models import device, face_analysis, providers_in_use

    print(f"numpy       {np.__version__}")
    print(f"opencv      {cv2.__version__}")
    print(f"onnxruntime {ort.__version__} providers={ort.get_available_providers()}")
    print(f"insightface {insightface.__version__}")
    try:
        import pytest

        print(f"pytest      {pytest.__version__}")
    except ImportError:  # only needed to run the tests
        print("pytest      not installed (only for the tests: pip install --no-deps -r requirements.txt)")

    dev = device()
    print(f"\nLoading {config.MODEL_PACK} on the {dev.label} (downloads on first run)...")
    t0 = time.perf_counter()
    fa = face_analysis()
    dev = device()  # the CPU after all if the models wouldn't load on the GPU
    print(f"Model loaded in {time.perf_counter() - t0:.1f}s: {sorted(fa.models)}")
    print(f"Device: {dev.label} (GATESENSE_DEVICE={dev.requested}); providers in use: {providers_in_use()}")
    if dev.warning:
        print(f"WARNING: {dev.warning}")

    from app import liveness

    bad = liveness.problems()
    state = "OK, SHA-256 verified" if not bad else ", ".join(f"{n} {why}" for n, why in bad.items())
    print(f"Liveness models ({'on' if config.LIVENESS_ENABLED else 'OFF'}): {state} in {config.LIVENESS_MODEL_DIR}")
    if bad:
        print("  run scripts\\get_models.py (they also download on a camera's first start)")

    source = config.CAMERAS[0]["source"]
    print(f"\nOpening camera {source!r}...")
    backend = cv2.CAP_DSHOW if isinstance(source, int) and sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(source, backend)
    if not cap.isOpened():
        print("ERROR: could not open camera")
        return 1
    # Discard a few frames so auto-exposure settles.
    frame = None
    for _ in range(10):
        ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        print("ERROR: could not read a frame")
        return 1
    print(f"Frame size  {frame.shape[1]}x{frame.shape[0]}, mean brightness {frame.mean():.0f}/255")

    scale = config.FRAME_WIDTH / frame.shape[1]
    small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame

    fa.get(small)  # warm-up run; the first inference is always slower
    t0 = time.perf_counter()
    faces = fa.get(small)
    ms = (time.perf_counter() - t0) * 1000

    print(f"\nFaces found: {len(faces)}")
    print(f"Detection + embedding time: {ms:.0f} ms on {small.shape[1]}x{small.shape[0]}")
    for i, f in enumerate(faces):
        w = int(f.bbox[2] - f.bbox[0])
        print(f"  face {i}: score={f.det_score:.2f} width={w}px embedding={f.normed_embedding.shape}")
    print("\nEnvironment OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
