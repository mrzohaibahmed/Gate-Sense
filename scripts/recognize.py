"""Attendance worker for one camera: recognize, track, vote and record IN/OUT events.

Run one instance per camera from app/config.py CAMERAS. Each frame (about
PROCESS_FPS per second) goes through: detect -> ByteTrack -> quality filter ->
embed the track's best frames -> match -> vote. Detection and tracking run on
a FRAME_WIDTH px copy; the faces are then scaled back to the camera's pixels,
so the quality filter (MIN_FACE_PX is camera pixels), the ArcFace alignment
and the snapshot use the full-resolution frame. When a
track's vote is won, an event is written to data/attendance.db with a face
snapshot in data/snapshots/, unless the person is still in the cool-down for
this camera. Every match decision is logged (time, track, name or unknown,
score, second-best score) for threshold tuning.

The worker reports its status in data/run/<camera>.json and exits cleanly when
data/run/<camera>.stop appears (see app/workers.py), so the dashboard's
Cameras page can show and stop it whether it was started there or here. It
reloads data/gallery.npz when enrollment changes it. A second worker for the
same camera, or for a webcam another worker already uses, refuses to start.

Live sources (webcam, RTSP) are read on a background thread (app.camera.LiveCapture),
so a camera that hangs or that another app holds can't freeze the worker; it
warns when the camera sends no frames or only black ones. The annotated frame
and the last decisions go to data/live/<camera>.jpg/.json for the dashboard's
live view (app/live.py), and are deleted when the worker stops.

Run from the project root:
    .venv\\Scripts\\python scripts\\recognize.py                                   # first camera (entrance, webcam)
    .venv\\Scripts\\python scripts\\recognize.py --camera exit --source clip.mp4   # exit camera from a clip
    .venv\\Scripts\\python scripts\\recognize.py --no-db                           # tune only, record nothing
Press Q or Esc in the window to quit.
"""

import argparse
import sys
import time
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app import config, db, frame_source, sources, workers  # noqa: E402
from app.attendance import Cooldown  # noqa: E402
from app.camera import is_live, mask_source, open_capture, parse_source  # noqa: E402
from app.detector import (  # noqa: E402
    Detector, check_quality, detection_scale, quality_score, resize_to_width, scale_detection,
)
from app.gallery import Gallery, Match  # noqa: E402
from app.images import crop_face, write_image  # noqa: E402
from app.live import CameraHealth, LiveWriter  # noqa: E402
from app.models import providers_in_use  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402
from app.tracker import FaceTracker, Sample, TrackState  # noqa: E402

GREEN, RED, GRAY, YELLOW = (0, 200, 0), (0, 0, 255), (160, 160, 160), (0, 220, 220)
FAILURES = {"no gallery", "could not open source", "video file not found", "lost the camera"}
STATS_SECONDS = 60  # log a summary of what the pipeline saw this often


def fmt_score(name: str | None, score: float | None) -> str:
    return "-" if score is None else f"{score:.3f} ({name})"


def log_match(stamp: str, track_id: int, m: Match) -> None:
    label = m.name if m.name else "unknown"
    verdict = "ACCEPT" if m.name else m.reason
    print(
        f"{stamp}  track{track_id:<4} {label:<12} score={fmt_score(m.best_name, m.score):<22} "
        f"second={fmt_score(m.second_name, m.second_score):<22} {verdict}"
    )


def draw(img, bbox, color, text, thickness: int = 2) -> None:
    x1, y1, x2, y2 = (int(v) for v in bbox)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
    cv2.putText(img, text, (x1, max(y1 - 8, 15)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)


def mirror_box(bbox, width: int):
    """The same box in a left-right mirrored image of `width` pixels."""
    x1, y1, x2, y2 = bbox
    return (width - x2, y1, width - x1, y2)


def header(img, text: str) -> None:
    cv2.rectangle(img, (0, 0), (img.shape[1], 24), (0, 0, 0), -1)
    cv2.putText(img, text, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)


def find_camera(name: str | None) -> dict:
    if name is None:
        return config.CAMERAS[0]
    for cam in config.CAMERAS:
        if cam["name"] == name:
            return cam
    raise SystemExit(f"Unknown camera {name!r}; config has: {[c['name'] for c in config.CAMERAS]}")


class Recorder:
    """Applies the cool-down, saves the snapshot and writes the event."""

    def __init__(self, cam: dict, source, use_db: bool):
        self.cam_name, self.direction = cam["name"], cam["direction"]
        self.conn = db.connect() if use_db else None
        last = {}
        if self.conn:
            self.camera_id = db.upsert_camera(self.conn, self.cam_name, source, self.direction)
            last = db.last_event_times(self.conn, self.cam_name)
        self.cooldown = Cooldown(config.COOLDOWN_SECONDS, last)

    def record(self, stamp: str, now: datetime, track: TrackState, name: str, votes: int) -> tuple[str, str]:
        """Returns (decision kind, detail) for the live view: event, cool-down or not saved."""
        best = track.best_sample(name)
        head = f"{stamp}  track{track.track_id:<4} {self.direction:<3} {name}"
        if not self.cooldown.allow(name, self.cam_name, now):
            print(f"{head}  ignored: cool-down ({config.COOLDOWN_SECONDS}s) since last {self.direction}")
            return "cool-down", f"{self.direction} already recorded in the last {config.COOLDOWN_SECONDS} s"
        if self.conn is None:
            print(f"{head}  EVENT (not saved, --no-db)  score={best.match.score:.3f}  votes {votes}/{len(track.samples)}")
            return "not saved", "--no-db"
        person_id = db.active_person_id(self.conn, name)  # looked up each time, so dashboard changes apply live
        if person_id is None:
            print(f"{head}  NOT SAVED: inactive in the dashboard, or not enrolled (run scripts\\enroll.py)")
            return "not saved", "person inactive or not enrolled"
        snap = config.SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / f"{now:%H%M%S}_{name}_{self.cam_name}.jpg"
        write_image(snap, best.snapshot)
        rel = snap.relative_to(config.DATA_DIR).as_posix()
        db.add_event(self.conn, person_id, self.camera_id, now, best.match.score, rel)
        print(f"{head}  EVENT saved  score={best.match.score:.3f}  votes {votes}/{len(track.samples)}  snapshot={rel}")
        return "event", f"{self.direction} saved (votes {votes}/{len(track.samples)})"


class Pipeline:
    """Detect, track, check quality, embed, match, vote and record one frame; returns the annotated view.

    Detection, tracking and the view use the FRAME_WIDTH copy; the quality filter, embedding and snapshot use
    the full-resolution frame, with each face's box and landmarks scaled back to it.

    With `mirror`, only the returned view is flipped left-right (like a mirror or a video call); detection,
    recognition and snapshots use the original frame, the same orientation as the enrollment photos.
    """

    def __init__(self, gallery: Gallery, detector, recognizer, recorder: Recorder, live: LiveWriter,
                 mirror: bool = False):
        self.gallery, self.detector, self.recognizer = gallery, detector, recognizer
        self.recorder, self.live = recorder, live
        self.mirror = mirror
        self.tracker = FaceTracker(config.PROCESS_FPS)
        self.tracks: dict[int, TrackState] = {}
        self.times = deque(maxlen=20)
        self.stats = Counter()
        self.frame_size: tuple[int, int] | None = None  # camera frame (width, height), logged when it changes

    def log_size(self, frame, small, stamp: str) -> None:
        """Log the camera and detection sizes on the first frame, and again if the camera's resolution changes."""
        size = (frame.shape[1], frame.shape[0])
        if size == self.frame_size:
            return
        self.frame_size = size
        full, det = f"{size[0]}x{size[1]}", f"{small.shape[1]}x{small.shape[0]}"
        if small is frame:
            print(f"{stamp}  frame size {full}: detection and recognition both at {full} (no resize)")
        else:
            print(f"{stamp}  frame size {full}: detection at {det} (scale {detection_scale(frame, small):.2f}), "
                  f"quality filter, embeddings and snapshots at the full {full}")

    def process(self, frame, t: float, now: datetime, stamp: str):
        t0 = time.perf_counter()
        self.live.full_frame(frame)  # the raw frame, for the enrollment wizard if it uses this camera
        small = resize_to_width(frame)
        scale = detection_scale(frame, small)
        self.log_size(frame, small, stamp)
        marks = []  # (bbox, color, text, thickness), drawn at the end onto the (possibly mirrored) view

        def mark(det, color, text, thickness=2):
            marks.append((det.bbox, color, text, thickness))

        dets = self.detector.detect(small)
        tracked = self.tracker.update(dets)
        self.stats["frames"] += 1
        self.stats["faces"] += len(dets)
        tracked_ids = {id(d) for _, d in tracked}
        for d in dets:
            if id(d) not in tracked_ids:
                mark(d, GRAY, "new face", 1)  # not confirmed by the tracker yet

        for tid, det in tracked:
            track = self.tracks.setdefault(tid, TrackState(tid))
            track.last_seen = t
            if track.decided:
                mark(det, GREEN, f"#{tid} {track.decided}")
                continue
            full = scale_detection(det, scale)  # the same face in the camera's pixels
            skip = check_quality(frame, full)
            if skip:
                print(f"{stamp}  track{tid:<4} {'skip':<12} {skip}")
                self.stats[f"skip: {skip.split(' (')[0]}"] += 1
                self.live.decision("skip", tid, detail=skip, face_px=full.width)
                mark(det, GRAY, f"#{tid} skip: {skip.split(' (')[0]}")
                continue
            q = quality_score(full)
            if not track.wants(q):
                mark(det, YELLOW, f"#{tid} ...")
                continue
            m = self.gallery.match(self.recognizer.embed(frame, full))
            log_match(stamp, tid, m)
            track.add(Sample(q, m, crop_face(frame, full.bbox, config.SNAPSHOT_SIZE)))
            if m.name:
                self.stats["matched"] += 1
                votes = sum(s.match.name == m.name for s in track.samples)
                self.live.decision("match", tid, m.name, m.score, m.second_score,
                                   f"vote {votes}/{config.VOTE_MIN_AGREE}", face_px=full.width)
            else:
                self.stats[f"unknown: {m.reason}"] += 1
                self.live.decision("unknown", tid, m.best_name, m.score, m.second_score, m.reason,
                                   face_px=full.width)
            mark(det, GREEN if m.name else RED, f"#{tid} {m.name or 'unknown'} {m.score:.2f}")
            won = track.vote()
            if won:
                track.decided = won[0]
                kind, detail = self.recorder.record(stamp, now, track, *won)
                self.stats[kind] += 1
                self.live.decision(kind, tid, won[0], track.best_sample(won[0]).match.score, detail=detail)

        for tid, track in list(self.tracks.items()):
            if t - track.last_seen > config.TRACK_LOST_SECONDS:
                if not track.decided and track.samples:
                    print(f"{stamp}  track{tid:<4} closed as unknown (votes: {track.votes_summary()})")
                    self.live.decision("closed", tid, detail=f"left without a decision ({track.votes_summary()})")
                del self.tracks[tid]

        # A new image either way, so the capture's buffer is never drawn on.
        # While the API streams this camera, draw the view from the full frame (up to STREAM_MAX_WIDTH) so the
        # stream is sharper; otherwise the detection-size copy, as before.
        base = small
        if self.live.watched() and frame.shape[1] > small.shape[1]:
            base = resize_to_width(frame, config.STREAM_MAX_WIDTH)
        view = cv2.flip(base, 1) if self.mirror else base.copy()
        k = view.shape[1] / small.shape[1]  # detection-copy pixels -> view pixels
        for bbox, color, text, thickness in marks:
            bbox = tuple(v * k for v in bbox)
            draw(view, mirror_box(bbox, view.shape[1]) if self.mirror else bbox, color, text, thickness)
        self.times.append(time.perf_counter() - t0)
        return view

    def ms_per_frame(self) -> float:
        return 1000 * sum(self.times) / len(self.times) if self.times else 0.0

    def stats_line(self) -> str:
        s = dict(self.stats)
        self.stats.clear()
        return "stats: " + (", ".join(f"{k} {v}" for k, v in s.items()) if s else "no frames processed")


def main() -> int:
    parser = argparse.ArgumentParser(description="GateSense attendance worker for one camera.")
    parser.add_argument("--camera", help="camera name from app/config.py CAMERAS (default: the first)")
    parser.add_argument("--source", help="override the camera's source: webcam index, video file or RTSP URL")
    parser.add_argument("--no-window", action="store_true", help="log only, don't show video")
    parser.add_argument("--no-db", action="store_true", help="don't write events or snapshots (for tuning)")
    parser.add_argument("--parent-pid", type=int, help="exit when this process ends (set by the dashboard)")
    args = parser.parse_args()
    cam = find_camera(args.camera)
    source = parse_source(args.source) if args.source is not None else cam["source"]

    pid = workers.running_elsewhere(cam["name"])
    if pid:
        print(f"ERROR: a worker for {cam['name']} is already running (pid {pid}).")
        return 1
    other = workers.webcam_conflict(cam["name"], source, config.CAMERAS)
    if other:
        print(f"ERROR: webcam {source} is already in use by the {other} worker.")
        return 1
    # Held until this process ends, so a second process (dashboard, API or command line) can't open this camera
    # or webcam even if it passed the checks above at the same moment.
    locks = workers.acquire_camera(cam["name"], source)
    if locks is None:
        print(f"ERROR: {cam['name']} (or its webcam) is already open in another worker process.")
        return 1

    runtime = workers.WorkerRuntime(cam["name"], source, args.parent_pid)
    live = LiveWriter(cam["name"])
    reason = "error"
    try:
        reason = run(args, cam, source, runtime, live)
    finally:
        runtime.close(reason)
        live.clear()
        for lock in locks:
            lock.release()
        print(f"Worker stopped: {reason}")
    return 1 if reason in FAILURES or reason.startswith("missing environment variable") else 0


def run(args, cam: dict, source, runtime: "workers.WorkerRuntime", live: LiveWriter) -> str:
    """The worker loop; returns why it stopped."""
    try:
        stream = sources.expand(source)  # ${VAR} -> value; only used to open the stream, never stored or shown
    except sources.MissingVariables as e:
        print(f"ERROR: {e}")
        return f"missing environment variable {', '.join(e.names)}"
    if not config.GALLERY_PATH.exists():
        print(f"No gallery at {config.GALLERY_PATH}. Enroll people first (dashboard or scripts\\enroll.py).")
        return "no gallery"
    gallery = Gallery.load()
    gallery_mtime = config.GALLERY_PATH.stat().st_mtime
    print(f"Camera {cam['name']} ({cam['direction']}), gallery: {gallery.counts()}")
    print(
        f"Threshold {config.MATCH_THRESHOLD}, margin {config.MATCH_MARGIN}, {config.PROCESS_FPS} fps, "
        f"best {config.BEST_FRAMES_PER_TRACK} frames, vote {config.VOTE_MIN_AGREE}, cool-down {config.COOLDOWN_SECONDS}s"
    )
    detector, recognizer = Detector(), Recognizer()
    print(f"Model {config.MODEL_PACK}, providers {providers_in_use()}")
    mirror = bool(cam.get("mirror"))
    if mirror:
        print("View mirrored left-right (recognition uses the original frame)")
    pipeline = Pipeline(gallery, detector, recognizer, Recorder(cam, source, use_db=not args.no_db), live, mirror)
    window = f"GateSense {cam['name']} ({cam['direction']})"
    label = f"{cam['name']} ({cam['direction']})"

    def housekeeping() -> str | None:
        """Status, stop request, gallery reload and periodic stats; returns a stop reason."""
        nonlocal gallery_mtime, last_gallery_check, last_stats
        runtime.tick()
        if stop := runtime.should_stop():
            return stop
        mono = time.monotonic()
        if mono - last_gallery_check >= config.GALLERY_RELOAD_SECONDS:
            last_gallery_check = mono
            pipeline.gallery, gallery_mtime = reload_gallery(pipeline.gallery, gallery_mtime)
        if mono - last_stats >= STATS_SECONDS:
            last_stats = mono
            print(f"{datetime.now():%H:%M:%S}  {pipeline.stats_line()}")
        return None

    def show(img, stamp: str) -> bool:
        """Header, live frame, optional window; True if the window was closed with Q/Esc."""
        header(img, f"{label}  {stamp}  {runtime.fps:.1f} fps  {pipeline.ms_per_frame():.0f} ms/frame")
        live.frame(img, runtime.fps, runtime.warning)
        if args.no_window:
            return False
        cv2.imshow(window, img)
        return cv2.waitKey(1) & 0xFF in (ord("q"), 27)

    last_gallery_check = last_stats = time.monotonic()
    try:
        if is_live(stream):
            return run_live(stream, runtime, live, pipeline, housekeeping, show)
        return run_file(stream, runtime, pipeline, housekeeping, show)
    except KeyboardInterrupt:
        return "interrupted"
    finally:
        cv2.destroyAllWindows()
        if pipeline.recorder.conn:
            pipeline.recorder.conn.close()
        if pipeline.times:
            print(f"Average processing time: {pipeline.ms_per_frame():.0f} ms/frame")


def run_live(source, runtime, live: LiveWriter, pipeline: Pipeline, housekeeping, show) -> str:
    """Webcam, RTSP or stream: newest frame from the background reader, at most PROCESS_FPS per second.

    RTSP sources (app.frame_source.FrameSource) reconnect by themselves and never end the worker;
    webcams keep the LiveCapture behaviour.
    """
    rtsp = frame_source.kind(source) == "rtsp"
    print(f"Source {mask_source(source)!r} ({'RTSP over TCP' if rtsp else 'live'}), opening...")
    capture = frame_source.open_live(source, log=print)
    runtime.health_provider = capture.health
    live.start()
    health = CameraHealth(hint="check the camera's power, network and address" if rtsp else None)
    interval = 1.0 / config.PROCESS_FPS
    last_seq, next_due, start, opened = 0, 0.0, time.monotonic(), False
    try:
        while True:
            if stop := housekeeping():
                return stop
            if capture.error:
                print(f"ERROR: {capture.error} ({mask_source(source)!r}).")
                return capture.error
            if capture.opened and not opened:
                opened = True
                print(f"Camera opened after {time.monotonic() - start:.1f} s\n")
            got = capture.latest(last_seq) if time.monotonic() >= next_due else None
            warning = health.update(got[1] if got else None)
            if rtsp and capture.state == "reconnecting":  # the reconnect reason says more than "no frames"
                warning = "camera reconnecting" + (f": {capture.last_error}" if capture.last_error else "")
            if warning != runtime.warning:
                print(f"{datetime.now():%H:%M:%S}  WARNING: {warning}" if warning
                      else f"{datetime.now():%H:%M:%S}  camera OK again")
                runtime.warning = warning
                live.write_info(runtime.fps, warning)
            if got is None:
                time.sleep(0.01)
                continue
            last_seq, frame = got
            next_due = time.monotonic() + interval
            now = datetime.now()
            stamp = now.strftime("%H:%M:%S.%f")[:-3]
            img = pipeline.process(frame, time.monotonic() - start, now, stamp)
            runtime.tick(processed=True)
            if show(img, stamp):
                return "window closed"
    finally:
        capture.stop()


def run_file(source, runtime, pipeline: Pipeline, housekeeping, show) -> str:
    """Video file: every Nth frame in order, timed by the video position."""
    if missing := workers.missing_file(source):
        print(f"ERROR: video file not found: {missing}")
        print("Put a clip there, or change this camera's source in app/config.py (webcam index or RTSP URL).")
        return "video file not found"
    try:
        cap = open_capture(source)
    except RuntimeError:
        print(f"ERROR: could not open video source {mask_source(source)!r}")
        return "could not open source"
    src_fps = cap.get(cv2.CAP_PROP_FPS)
    if src_fps <= 0:
        src_fps = 30.0
    step = max(1, round(src_fps / config.PROCESS_FPS))
    print(f"Source {mask_source(source)!r} at {src_fps:.0f} fps\n")
    pipeline.live.start()
    start_dt, frame_idx = datetime.now(), 0
    file_health = {"kind": "file", "state": "live", "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None,
                   "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None, "input_fps": round(src_fps, 1),
                   "last_frame_at": None, "reconnects": 0, "reconnects_today": 0, "last_error": None,
                   "live_since": time.time()}
    runtime.health_provider = lambda: file_health
    try:
        while True:
            if stop := housekeeping():
                return stop
            ok, frame = cap.read()
            if not ok:
                print("End of video.")
                return "end of video"
            frame_idx += 1
            if (frame_idx - 1) % step:
                continue
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            file_health["last_frame_at"] = time.time()
            now = start_dt + timedelta(seconds=t)  # video time, so the cool-down behaves as in real life
            stamp = f"t={t:8.2f}s"
            img = pipeline.process(frame, t, now, stamp)
            runtime.tick(processed=True)
            if show(img, stamp):
                return "window closed"
    finally:
        cap.release()


def reload_gallery(gallery: Gallery, mtime: float) -> tuple[Gallery, float]:
    """Pick up enrollment changes made while running (the dashboard rewrites gallery.npz)."""
    try:
        new_mtime = config.GALLERY_PATH.stat().st_mtime
        if new_mtime == mtime:
            return gallery, mtime
        gallery = Gallery.load()
        print(f"Gallery reloaded: {gallery.counts()}")
        return gallery, new_mtime
    except (OSError, ValueError) as e:  # missing or mid-replace: keep the current one
        print(f"Gallery reload skipped: {e}")
        return gallery, mtime


if __name__ == "__main__":
    sys.exit(main())
