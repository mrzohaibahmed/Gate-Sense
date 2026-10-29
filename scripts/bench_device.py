"""Measure the models on the CPU and on the NVIDIA GPU (CUDA) of this PC: ms per frame for what a camera worker does
with a frame that has a new face (detect on the FRAME_WIDTH copy, embed the largest face from the full frame, check
its liveness), and whether both devices give the same embedding.

The same buffalo_s and liveness model files run on both. Without a usable GPU only the CPU is measured and the
reason is printed. Give it a photo with faces, ideally from the gate camera (default: InsightFace's sample photo
with six faces); --width scales it to a gate camera's resolution first. The liveness check is left out until its
models are downloaded (scripts\\get_models.py).

Run from the project root:
    .venv\\Scripts\\python scripts\\bench_device.py
    .venv\\Scripts\\python scripts\\bench_device.py photo.jpg --width 1920 --frames 200
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import config, liveness, models  # noqa: E402
from app.detector import Detector, detection_scale, resize_to_width, scale_detection  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402

STAGES = ("detect", "embed", "liveness")


def sample_photo() -> Path:
    from insightface.utils import face_align

    return Path(face_align.__file__).resolve().parent.parent / "data" / "images" / "t1.jpg"


def load(dev: models.Device):
    """(detector, recognizer, liveness or None) on `dev`; None if the GPU's sessions didn't start on CUDA."""
    fa = models.load_face_analysis(dev)
    if dev.name == "cuda" and models.lost_cuda(m.session for m in fa.models.values()):
        return None
    checker = None
    if not liveness.problems():
        checker = liveness.Liveness([(liveness.session(path, dev), scale) for path, scale in liveness.model_files()])
        if dev.name == "cuda" and models.lost_cuda(s for s, *_ in checker.models):
            return None
    return Detector(fa), Recognizer(fa), checker


def run_frame(detector, recognizer, checker, frame) -> tuple[dict[str, float], np.ndarray]:
    """ms per stage for one frame, and the largest face's embedding."""
    times = {}
    t0 = time.perf_counter()
    small = resize_to_width(frame)
    dets = detector.detect(small)
    times["detect"] = (time.perf_counter() - t0) * 1000
    face = scale_detection(dets[0], detection_scale(frame, small))
    t0 = time.perf_counter()
    emb = recognizer.embed(frame, face)
    times["embed"] = (time.perf_counter() - t0) * 1000
    if checker is not None:
        t0 = time.perf_counter()
        checker.check(frame, face.bbox)
        times["liveness"] = (time.perf_counter() - t0) * 1000
    return times, emb


def measure(loaded, frame, frames: int) -> tuple[dict[str, float], float, np.ndarray]:
    """(median ms per stage, median ms per frame, embedding) over `frames` frames after a warm-up (the first runs,
    on a GPU especially, are slower)."""
    detector, recognizer, checker = loaded
    for _ in range(10):
        run_frame(detector, recognizer, checker, frame)
    per_stage, totals = {s: [] for s in STAGES}, []
    for _ in range(frames):
        times, emb = run_frame(detector, recognizer, checker, frame)
        for stage, ms in times.items():
            per_stage[stage].append(ms)
        totals.append(sum(times.values()))
    return {s: statistics.median(v) for s, v in per_stage.items() if v}, statistics.median(totals), emb


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the face models on the CPU and on the NVIDIA GPU.")
    parser.add_argument("image", nargs="?", help="a photo with faces (default: InsightFace's sample photo)")
    parser.add_argument("--width", type=int, help="scale the photo to this width first (e.g. 1920 for a gate camera)")
    parser.add_argument("--frames", type=int, default=100, help="frames measured on each device")
    args = parser.parse_args()
    path = Path(args.image) if args.image else sample_photo()
    frame = cv2.imread(str(path))
    if frame is None:
        print(f"ERROR: can't read {path}")
        return 1
    if args.width:
        frame = cv2.resize(frame, None, fx=args.width / frame.shape[1], fy=args.width / frame.shape[1])

    devices = [models.Device("cpu", "cpu")]
    gpu = models.resolve_device("cuda")
    if gpu.name == "cuda":
        devices.append(gpu)
    else:
        print(f"GPU: not measured, {gpu.warning}")
    print(f"Photo {path.name}, {frame.shape[1]}x{frame.shape[0]}; {config.MODEL_PACK}, detection at "
          f"{config.FRAME_WIDTH} px, {models.onnx_threads()} ONNX threads; {args.frames} frames per device")
    if liveness.problems():
        print("Liveness: not measured, its models aren't downloaded (scripts\\get_models.py)")

    results = {}
    for dev in devices:
        loaded = load(dev)
        if loaded is None:
            print(f"{dev.label}: not measured, its sessions started on the CPU (CUDA 12.8+ and cuDNN 9 must be "
                  f"installed)")
            continue
        if not loaded[0].detect(resize_to_width(frame)):
            print("ERROR: no face found in the photo")
            return 1
        stages, total, emb = measure(loaded, frame, args.frames)
        results[dev.name] = (total, emb)
        parts = ", ".join(f"{s} {ms:5.1f}" for s, ms in stages.items())
        print(f"  {dev.label:<18} {total:6.1f} ms/frame = {1000 / total:5.1f} fps at most ({parts} ms)")
    if len(results) == 2:
        (cpu_ms, cpu_emb), (gpu_ms, gpu_emb) = results["cpu"], results["cuda"]
        print(f"  GPU vs CPU: {cpu_ms / gpu_ms:.1f}x as fast; embedding cosine {float(cpu_emb @ gpu_emb):.5f} "
              f"(same model, so above 0.99)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
