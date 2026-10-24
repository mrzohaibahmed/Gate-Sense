"""Measure what the liveness check costs on this PC: ms per face, and the worker's ms per frame and highest fps
with it off and on.

The worker processes at most PROCESS_FPS frames a second; this shows how much headroom is left. The "on" run is the
worst case: every frame has an undecided face, so each frame is detected, liveness-checked and embedded (once a
track is decided, neither runs again for it). Off and on run in alternating blocks of --frames frames (--rounds
each), as a worker runs one or the other: frame by frame turns would let the liveness models' threads slow the "off"
frames too. "best" is the fastest block's median, the closest to the pure computing cost on a busy or throttling CPU.
Give it a photo with one real face, ideally from the gate camera; --width also tests it scaled to a gate camera's
resolution (the crops come from the full frame).

Run from the project root:
    .venv\\Scripts\\python scripts\\bench_liveness.py photo.jpg
    .venv\\Scripts\\python scripts\\bench_liveness.py photo.jpg --width 1920 --frames 100
"""

import argparse
import contextlib
import io
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402

from app.attendance import Cooldown  # noqa: E402
from app.detector import Detector, detection_scale, resize_to_width, scale_detection  # noqa: E402
from app.gallery import Gallery  # noqa: E402
from app.live import LiveWriter  # noqa: E402
from app.liveness import Liveness  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402


class NoRecorder:
    conn = None
    cooldown = Cooldown(0)

    def save_spoof(self, *args):
        return None


def block_ms(pipeline, frame, frames: int) -> float:
    """Median ms per process() call over `frames` frames; the tracks are forgotten after each frame, so every frame
    is a new undecided face."""
    times = []
    for i in range(frames + 5):
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):  # the per-frame decision lines
            pipeline.process(frame, i * 0.125, datetime.now(), f"bench {i}")
        if i >= 5:  # the first calls warm up onnxruntime
            times.append((time.perf_counter() - t0) * 1000)
        pipeline.tracks.clear()
    return statistics.median(times)


def summary(times: list[float]) -> tuple[float, float]:
    """(median, 10th percentile)"""
    return statistics.median(times), statistics.quantiles(times, n=10)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the liveness check's cost on this PC.")
    parser.add_argument("image", help="a photo with one real face")
    parser.add_argument("--width", type=int, help="also scale the photo to this width (e.g. 1920 for a gate camera)")
    parser.add_argument("--frames", type=int, default=50, help="frames per block")
    parser.add_argument("--rounds", type=int, default=4, help="blocks with liveness off, and as many on")
    args = parser.parse_args()
    image = cv2.imread(args.image)
    if image is None:
        print(f"ERROR: can't read {args.image}")
        return 1

    import recognize  # the worker's Pipeline (scripts/recognize.py)

    detector, recognizer, checker = Detector(), Recognizer(), Liveness()
    sizes = [image] + ([cv2.resize(image, None, fx=args.width / image.shape[1], fy=args.width / image.shape[1])]
                       if args.width else [])
    live_dir = Path(tempfile.mkdtemp(prefix="gatesense-bench-"))
    for frame in sizes:
        small = resize_to_width(frame)
        dets = detector.detect(small)
        if len(dets) != 1:
            print(f"ERROR: the photo needs exactly one face (found {len(dets)})")
            return 1
        face = scale_detection(dets[0], detection_scale(frame, small))
        gallery = Gallery.from_people({"me": [recognizer.embed(frame, face)]})
        for _ in range(10):
            checker.check(frame, face.bbox)
        per_face = [checker.check(frame, face.bbox).ms for _ in range(args.frames * 2)]
        result = checker.check(frame, face.bbox)
        print(f"\n{frame.shape[1]}x{frame.shape[0]}, face {face.width}px, live score {result.score:.3f} "
              f"({'live' if result.live else 'SPOOF'})")
        median, fast = summary(per_face)
        print(f"  liveness check (2 models): median {median:.1f} ms/face, fast {fast:.1f} ms")
        blocks = {"off": [], "on": []}
        for r in range(args.rounds):
            for label in ("off", "on") if r % 2 == 0 else ("on", "off"):
                live = checker if label == "on" else None
                pipeline = recognize.Pipeline(gallery, detector, recognizer, NoRecorder(),
                                              LiveWriter("bench", live_dir), liveness=live)
                blocks[label].append(block_ms(pipeline, frame, args.frames))
        for label, times in blocks.items():
            best, typical = min(times), statistics.median(times)
            print(f"  worker, liveness {label:<3}: best {best:6.1f} ms/frame = {1000 / best:5.1f} fps at most; "
                  f"typical {typical:6.1f} ms = {1000 / typical:5.1f} fps")
        print(f"  extra with an undecided face: best {min(blocks['on']) - min(blocks['off']):+.1f} ms/frame, "
              f"typical {statistics.median(blocks['on']) - statistics.median(blocks['off']):+.1f} ms/frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
