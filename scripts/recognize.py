"""Attendance worker for one camera: recognize, track, vote and record IN/OUT events.

Run one instance per camera from app/config.py CAMERAS. Each frame (about
PROCESS_FPS per second, ~FRAME_WIDTH px) goes through: detect -> ByteTrack ->
quality filter -> embed the track's best frames -> match -> vote. When a
track's vote is won, an event is written to data/attendance.db with a face
snapshot in data/snapshots/, unless the person is still in the cool-down for
this camera. Every match decision is logged (time, track, name or unknown,
score, second-best score) for threshold tuning.

The worker reports its status in data/run/<camera>.json and exits cleanly when
data/run/<camera>.stop appears (see app/workers.py), so the dashboard's
Cameras page can show and stop it whether it was started there or here. It
reloads data/gallery.npz when enrollment changes it. A second worker for the
same camera, or for a webcam another worker already uses, refuses to start.

Run from the project root:
    .venv\\Scripts\\python scripts\\recognize.py                                   # first camera (entrance, webcam)
    .venv\\Scripts\\python scripts\\recognize.py --camera exit --source clip.mp4   # exit camera from a clip
    .venv\\Scripts\\python scripts\\recognize.py --no-db                           # tune only, record nothing
Press Q or Esc in the window to quit.
"""

import argparse
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app import config, db, workers  # noqa: E402
from app.attendance import Cooldown  # noqa: E402
from app.camera import is_webcam, mask_source, open_capture, parse_source  # noqa: E402
from app.detector import Detector, check_quality, quality_score, resize_to_width  # noqa: E402
from app.gallery import Gallery, Match  # noqa: E402
from app.images import crop_face, write_image  # noqa: E402
from app.models import providers_in_use  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402
from app.tracker import FaceTracker, Sample, TrackState  # noqa: E402

GREEN, RED, GRAY, YELLOW = (0, 200, 0), (0, 0, 255), (160, 160, 160), (0, 220, 220)


def fmt_score(name: str | None, score: float | None) -> str:
    return "-" if score is None else f"{score:.3f} ({name})"


def log_match(stamp: str, track_id: int, m: Match) -> None:
    label = m.name if m.name else "unknown"
    verdict = "ACCEPT" if m.name else m.reason
    print(
        f"{stamp}  track{track_id:<4} {label:<12} score={fmt_score(m.best_name, m.score):<22} "
        f"second={fmt_score(m.second_name, m.second_score):<22} {verdict}"
    )


def draw(img, det, color, text) -> None:
    x1, y1, x2, y2 = det.bbox.astype(int)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
    cv2.putText(img, text, (x1, max(y1 - 8, 15)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)


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

    def record(self, stamp: str, now: datetime, track: TrackState, name: str, votes: int) -> None:
        best = track.best_sample(name)
        head = f"{stamp}  track{track.track_id:<4} {self.direction:<3} {name}"
        if not self.cooldown.allow(name, self.cam_name, now):
            print(f"{head}  ignored: cool-down ({config.COOLDOWN_SECONDS}s) since last {self.direction}")
            return
        if self.conn is None:
            print(f"{head}  EVENT (not saved, --no-db)  score={best.match.score:.3f}  votes {votes}/{len(track.samples)}")
            return
        person_id = db.active_person_id(self.conn, name)  # looked up each time, so dashboard changes apply live
        if person_id is None:
            print(f"{head}  NOT SAVED: inactive in the dashboard, or not enrolled (run scripts\\enroll.py)")
            return
        snap = config.SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / f"{now:%H%M%S}_{name}_{self.cam_name}.jpg"
        write_image(snap, best.snapshot)
        rel = snap.relative_to(config.DATA_DIR).as_posix()
        db.add_event(self.conn, person_id, self.camera_id, now, best.match.score, rel)
        print(f"{head}  EVENT saved  score={best.match.score:.3f}  votes {votes}/{len(track.samples)}  snapshot={rel}")


FAILURES = {"no gallery", "could not open source", "lost the camera"}


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

    runtime = workers.WorkerRuntime(cam["name"], source, args.parent_pid)
    reason = "error"
    try:
        reason = run(args, cam, source, runtime)
    finally:
        runtime.close(reason)
        print(f"Worker stopped: {reason}")
    return 1 if reason in FAILURES else 0


def run(args, cam: dict, source, runtime: "workers.WorkerRuntime") -> str:
    """The worker loop; returns why it stopped."""
    if not config.GALLERY_PATH.exists():
        print(f"No gallery at {config.GALLERY_PATH}. Enroll people first (dashboard or scripts\\enroll.py).")
        return "no gallery"
    gallery = Gallery.load()
    gallery_mtime = config.GALLERY_PATH.stat().st_mtime
    last_gallery_check = time.monotonic()
    print(f"Camera {cam['name']} ({cam['direction']}), gallery: {gallery.counts()}")
    print(
        f"Threshold {config.MATCH_THRESHOLD}, margin {config.MATCH_MARGIN}, {config.PROCESS_FPS} fps, "
        f"best {config.BEST_FRAMES_PER_TRACK} frames, vote {config.VOTE_MIN_AGREE}, cool-down {config.COOLDOWN_SECONDS}s"
    )

    detector, recognizer = Detector(), Recognizer()
    print(f"Model {config.MODEL_PACK}, providers {providers_in_use()}")
    recorder = Recorder(cam, source, use_db=not args.no_db)

    try:
        cap = open_capture(source)
    except RuntimeError:
        print(f"ERROR: could not open video source {mask_source(source)!r}")
        return "could not open source"
    webcam = is_webcam(source)
    # Files: process every Nth frame. Live sources: process at most PROCESS_FPS per second.
    src_fps = cap.get(cv2.CAP_PROP_FPS)
    if src_fps <= 0:  # webcams often report 0 or -1
        src_fps = 30.0
    step = 1 if webcam else max(1, round(src_fps / config.PROCESS_FPS))
    interval = 1.0 / config.PROCESS_FPS
    print(f"Source {mask_source(source)!r} at {src_fps:.0f} fps\n")

    tracker = FaceTracker(config.PROCESS_FPS)
    tracks: dict[int, TrackState] = {}
    window = f"GateSense {cam['name']} ({cam['direction']})"
    last_processed = -interval
    frame_idx = 0
    start, start_dt = time.monotonic(), datetime.now()
    times = deque(maxlen=20)
    reason = "stopped"

    try:
        while True:
            runtime.tick()
            if stop := runtime.should_stop():
                reason = stop
                break
            if time.monotonic() - last_gallery_check >= config.GALLERY_RELOAD_SECONDS:
                last_gallery_check = time.monotonic()
                gallery, gallery_mtime = reload_gallery(gallery, gallery_mtime)

            ok, frame = cap.read()
            if not ok:
                print("End of video." if not webcam else "ERROR: lost the camera.")
                reason = "lost the camera" if webcam else "end of video"
                break
            frame_idx += 1

            if webcam:
                t = time.monotonic() - start
                if t - last_processed < interval:
                    continue
                now = datetime.now()
                stamp = now.strftime("%H:%M:%S.%f")[:-3]
            else:
                if (frame_idx - 1) % step:
                    continue
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                now = start_dt + timedelta(seconds=t)  # video time, so the cool-down behaves as in real life
                stamp = f"t={t:8.2f}s"
            last_processed = t

            t0 = time.perf_counter()
            small = resize_to_width(frame)
            for tid, det in tracker.update(detector.detect(small)):
                track = tracks.setdefault(tid, TrackState(tid))
                track.last_seen = t
                if track.decided:
                    draw(small, det, GREEN, f"#{tid} {track.decided}")
                    continue
                reason = check_quality(small, det)
                if reason:
                    print(f"{stamp}  track{tid:<4} {'skip':<12} {reason}")
                    draw(small, det, GRAY, f"#{tid} skip")
                    continue
                q = quality_score(det)
                if not track.wants(q):
                    draw(small, det, YELLOW, f"#{tid} ...")
                    continue
                m = gallery.match(recognizer.embed(small, det))
                log_match(stamp, tid, m)
                track.add(Sample(q, m, crop_face(small, det.bbox, config.SNAPSHOT_SIZE)))
                draw(small, det, GREEN if m.name else RED, f"#{tid} {m.name or 'unknown'} {m.score:.2f}")
                won = track.vote()
                if won:
                    track.decided = won[0]
                    recorder.record(stamp, now, track, *won)

            for tid, track in list(tracks.items()):
                if t - track.last_seen > config.TRACK_LOST_SECONDS:
                    if not track.decided and track.samples:
                        print(f"{stamp}  track{tid:<4} closed as unknown (votes: {track.votes_summary()})")
                    del tracks[tid]
            times.append(time.perf_counter() - t0)
            runtime.tick(processed=True)

            if not args.no_window:
                ms = 1000 * sum(times) / len(times)
                cv2.putText(small, f"{ms:.0f} ms/frame", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, YELLOW, 1)
                cv2.imshow(window, small)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    reason = "window closed"
                    break
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        cap.release()
        cv2.destroyAllWindows()
        if recorder.conn:
            recorder.conn.close()
    if times:
        print(f"Average processing time: {1000 * sum(times) / len(times):.0f} ms/frame")
    return reason


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
