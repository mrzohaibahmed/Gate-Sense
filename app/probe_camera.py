"""Probe a camera: is the picture good enough for recognition?

    .venv\\Scripts\\python -m app.probe_camera entrance                 # a camera name from config.CAMERAS
    .venv\\Scripts\\python -m app.probe_camera "rtsp://user:pass@ip:554/Streaming/Channels/101"
    .venv\\Scripts\\python -m app.probe_camera 0 --seconds 10           # webcam index, or a video file path

Opens the source through FrameSource (RTSP: FFmpeg over TCP; webcams and files:
the normal OpenCV opener), runs for 20 seconds and prints the codec, resolution,
input fps, time per frame, and the width and score of every detected face. Like
the worker, it detects on a FRAME_WIDTH copy and measures faces in camera pixels;
the summary warns when the median face is narrower than MIN_FACE_PX camera pixels
(the quality filter's limit). One snapshot with face boxes is saved
to data/probe/<camera>_<time>.jpg (git-ignored: it may show faces). Camera
passwords are masked in all output. Runs alongside nothing else on a webcam:
stop that camera's worker first.
"""

import argparse
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2

from app import config, sources
from app.camera import mask_source, open_capture, parse_source
from app.detector import detection_scale, resize_to_width, scale_detection
from app.frame_source import FrameSource, kind, open_rtsp
from app.images import write_image

SMALL_FACES = "faces too small: zoom in, move the camera closer or use 1920x1080"


@dataclass
class ProbeResult:
    label: str
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    input_fps: float = 0.0
    reported_fps: float | None = None
    read_ms: float = 0.0
    detect_ms: float = 0.0
    frames: int = 0
    faces: list[tuple[float, int, int, float]] = field(default_factory=list)  # (t, camera px, processed px, score)
    snapshot: Path | None = None

    @property
    def median_width(self) -> float | None:
        """Median face width in camera pixels (what the worker's quality filter checks)."""
        return statistics.median(f[1] for f in self.faces) if self.faces else None

    @property
    def too_small(self) -> bool:
        return self.median_width is not None and self.median_width < config.MIN_FACE_PX


def resolve(target: str) -> tuple[str, str | int]:
    """Camera name or source -> (label for file names, source)."""
    for cam in config.CAMERAS:
        if cam["name"] == target:
            return cam["name"], cam["source"]
    source = parse_source(target)
    k = kind(source)
    if k == "webcam":
        return f"webcam{source}", source
    if k in ("rtsp", "stream"):
        host = re.sub(r"^[a-z]+://([^@/]*@)?", "", str(source), flags=re.I).split("/")[0].split(":")[0]
        return f"{k}_{re.sub(r'[^A-Za-z0-9.-]', '_', host) or 'camera'}", source
    return f"file_{re.sub(r'[^A-Za-z0-9.-]', '_', Path(str(source)).stem)}", source


def draw_faces(frame, faces_px) -> None:
    for (x1, y1, x2, y2), width, score in faces_px:
        cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 200, 0), 2)
        cv2.putText(frame, f"{width}px {score:.2f}", (int(x1), max(int(y1) - 8, 15)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 0), 2)


def probe(source, label: str, seconds: float, detector, frames=None, out=print,
          clock=time.monotonic, probe_dir: Path | None = None) -> ProbeResult:
    """Read the newest frames for `seconds`, detect faces on each, print as we go; returns the numbers."""
    shown = mask_source(source)
    if frames is None:
        opener = open_rtsp if kind(source) == "rtsp" else open_capture
        frames = FrameSource(source, opener=opener, log=lambda line: out(mask_source(line)))
    result = ProbeResult(label)
    detect_times: list[float] = []
    best = None  # (faces, frame, boxes) with the most faces, for the snapshot
    last_frame = None
    out(f"Probing {shown} for {seconds:g} s ...")
    start = clock()
    last_seq = 0
    try:
        while clock() - start < seconds:
            got = frames.latest(last_seq)
            if got is None:
                time.sleep(0.005)
                continue
            last_seq, frame = got
            t = clock() - start
            t0 = time.perf_counter()
            small = resize_to_width(frame)
            dets = detector.detect(small)
            detect_times.append(time.perf_counter() - t0)
            result.frames += 1
            last_frame = frame
            scale = detection_scale(frame, small)
            boxes = []
            for d in dets:
                full = scale_detection(d, scale)  # the same face in camera pixels, as in the worker
                result.faces.append((t, full.width, d.width, d.score))
                out(f"  t={t:5.1f}s  face width {full.width:4d} px (camera), {d.width:3d} px (processed)  "
                    f"score {d.score:.2f}")
                boxes.append((full.bbox, full.width, d.score))
            if boxes and (best is None or len(boxes) > len(best[2])):
                best = (len(boxes), frame, boxes)
    finally:
        stop = getattr(frames, "stop", None)
        if stop:
            stop()

    result.codec = getattr(frames, "codec", None)
    result.reported_fps = getattr(frames, "reported_fps", None)
    result.input_fps = frames.fps
    result.read_ms = getattr(frames, "read_ms", 0.0)
    if last_frame is not None:
        result.height, result.width = last_frame.shape[:2]
    result.detect_ms = 1000 * statistics.mean(detect_times) if detect_times else 0.0

    snap = best[1].copy() if best else (last_frame.copy() if last_frame is not None else None)
    if snap is not None:
        if best:
            draw_faces(snap, best[2])
        folder = probe_dir or (config.DATA_DIR / "probe")
        result.snapshot = folder / f"{label}_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        write_image(result.snapshot, snap)
    report(result, shown, out)
    return result


def report(r: ProbeResult, shown: str, out=print) -> None:
    out("")
    out(f"Camera     {shown}")
    if not r.frames:
        out("No frames received: check the address, password, network or that no worker is using the webcam.")
        return
    out(f"Codec      {r.codec or 'unknown'}")
    out(f"Resolution {r.width}x{r.height}")
    reported = f" (camera reports {r.reported_fps:.1f})" if r.reported_fps else ""
    out(f"Input fps  {r.input_fps:.1f}{reported}")
    out(f"Per frame  read+decode {r.read_ms:.1f} ms, detection {r.detect_ms:.1f} ms "
        f"(at {config.FRAME_WIDTH} px), together {r.read_ms + r.detect_ms:.1f} ms")
    out("           (read+decode runs on its own thread and, for a live camera, includes waiting for the next frame)")
    out(f"Frames     {r.frames} processed, {len(r.faces)} face detections")
    if r.median_width is None:
        out("Faces      none detected: stand or walk where people will pass the camera.")
    else:
        processed = statistics.median(f[2] for f in r.faces)
        out(f"Faces      median width {r.median_width:.0f} px in the camera image ({processed:.0f} px in the "
            f"{config.FRAME_WIDTH} px detection frame); quality filter needs {config.MIN_FACE_PX} camera px")
        if r.too_small:
            out(f"WARNING: {SMALL_FACES}")
            out("         (The quality filter and recognition use the camera's full resolution, so a higher "
                "resolution, zoom or a closer camera all help.)")
    if r.snapshot:
        out(f"Snapshot   {r.snapshot}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.probe_camera", description=__doc__.splitlines()[0])
    parser.add_argument("target", help="camera name from config.CAMERAS, webcam index, RTSP URL or video file")
    parser.add_argument("--seconds", type=float, default=20.0, help="how long to probe (default 20)")
    args = parser.parse_args(argv)
    label, source = resolve(args.target)
    try:
        stream = sources.expand(source)  # ${VAR} placeholders (camera passwords from .env)
    except sources.MissingVariables as e:
        print(f"ERROR: {e}")
        return 1

    from app.detector import Detector  # loads the models; kept out of import time for tests

    result = probe(stream, label, args.seconds, Detector())
    return 0 if result.frames else 1


if __name__ == "__main__":
    sys.exit(main())
