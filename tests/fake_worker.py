"""Stand-in for scripts/recognize.py in worker tests: same CLI and file protocol, no camera or models.

With --frames it reads synthetic frames from FakeFrameSource (the newest-frame interface of
app.camera.LiveCapture / app.frame_source.FrameSource) and writes them to the live view, with the
source's health in the status file, like a real worker on a camera. It takes the same camera lock
as recognize.py, so a second process for the camera exits with an error.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, live, workers  # noqa: E402

config.WORKER_STATUS_SECONDS = 0.2  # report quickly so tests run fast

parser = argparse.ArgumentParser()
parser.add_argument("--camera", required=True)
parser.add_argument("--no-window", action="store_true")
parser.add_argument("--parent-pid", type=int)
parser.add_argument("--run-dir")
parser.add_argument("--ignore-stop", action="store_true", help="simulate a hung worker")
parser.add_argument("--crash", action="store_true", help="die right after starting")
parser.add_argument("--source", help="lock this webcam index too, like recognize.py")
parser.add_argument("--frames", action="store_true", help="read frames from a fake frame source")
parser.add_argument("--live-dir")
parser.add_argument("--error", help="last error the fake frame source reports")
parser.add_argument("--decisions", action="store_true", help="with --frames: log a match and a skip now and then")
args = parser.parse_args()


class FakeFrameSource:
    """Synthetic 64x48 frames, newest first, with a health dict like FrameSource.health()."""

    def __init__(self, error: str | None = None):
        self.seq, self.last_frame_at, self.error, self.since = 0, None, error, time.time()

    def latest(self, after: int = 0):
        self.seq += 1
        self.last_frame_at = time.time()
        return self.seq, np.full((48, 64, 3), self.seq * 7 % 256, np.uint8)

    def health(self) -> dict:
        return {"kind": "fake", "state": "live", "width": 64, "height": 48, "input_fps": 20.0,
                "last_frame_at": self.last_frame_at, "reconnects": 0, "reconnects_today": 0,
                "last_error": self.error, "live_since": self.since}


locks = workers.acquire_camera(args.camera, args.source, args.run_dir)
if locks is None:
    print(f"ERROR: {args.camera} (or its webcam) is already open in another worker process.", flush=True)
    sys.exit(1)
runtime = workers.WorkerRuntime(args.camera, None, args.parent_pid, args.run_dir)
source = writer = None
if args.frames:
    source = FakeFrameSource(args.error)
    runtime.health_provider = source.health
    writer = live.LiveWriter(args.camera, live_dir=args.live_dir, interval=0.1)
print(f"fake worker {args.camera} started", flush=True)
if args.crash:
    os._exit(3)
reason = "stopped"
try:
    while True:
        if source:
            seq, frame = source.latest()
            if args.decisions and seq % 5 == 0:
                writer.decision("match", seq, "waqas", 0.71, 0.42, "vote 1/3", face_px=150)
                writer.decision("skip", seq + 1, detail="too small (60px < 80)", face_px=60)
            writer.full_frame(frame)
            writer.frame(frame, runtime.fps)
        runtime.tick(processed=True)
        stop = runtime.should_stop()
        if stop and not args.ignore_stop:
            reason = stop
            break
        time.sleep(0.05)
finally:
    runtime.close(reason)
    if writer:
        writer.clear()
    print(f"Worker stopped: {reason}", flush=True)
