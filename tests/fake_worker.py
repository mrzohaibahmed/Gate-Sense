"""Stand-in for scripts/recognize.py in worker tests: same CLI and file protocol, no camera or models."""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, workers  # noqa: E402

config.WORKER_STATUS_SECONDS = 0.2  # report quickly so tests run fast

parser = argparse.ArgumentParser()
parser.add_argument("--camera", required=True)
parser.add_argument("--no-window", action="store_true")
parser.add_argument("--parent-pid", type=int)
parser.add_argument("--run-dir")
parser.add_argument("--ignore-stop", action="store_true", help="simulate a hung worker")
parser.add_argument("--crash", action="store_true", help="die right after starting")
args = parser.parse_args()

runtime = workers.WorkerRuntime(args.camera, None, args.parent_pid, args.run_dir)
print(f"fake worker {args.camera} started", flush=True)
if args.crash:
    os._exit(3)
reason = "stopped"
try:
    while True:
        runtime.tick(processed=True)
        stop = runtime.should_stop()
        if stop and not args.ignore_stop:
            reason = stop
            break
        time.sleep(0.05)
finally:
    runtime.close(reason)
    print(f"Worker stopped: {reason}", flush=True)
