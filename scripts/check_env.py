"""Environment check.

Imports every dependency, loads the InsightFace buffalo_s model on CPU
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
    import pytest
    from insightface.app import FaceAnalysis

    print(f"numpy       {np.__version__}")
    print(f"opencv      {cv2.__version__}")
    print(f"onnxruntime {ort.__version__} providers={ort.get_available_providers()}")
    print(f"insightface {insightface.__version__}")
    print(f"pytest      {pytest.__version__}")

    print(f"\nLoading {config.MODEL_PACK} on CPU (downloads on first run)...")
    t0 = time.perf_counter()
    fa = FaceAnalysis(
        name=config.MODEL_PACK,
        providers=config.PROVIDERS,
        allowed_modules=["detection", "recognition"],
    )
    fa.prepare(ctx_id=config.CTX_ID, det_size=config.DET_SIZE, det_thresh=config.DET_THRESHOLD)
    print(f"Model loaded in {time.perf_counter() - t0:.1f}s: {sorted(fa.models)}")

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
