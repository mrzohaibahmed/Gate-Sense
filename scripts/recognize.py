"""Attendance worker for one camera: recognize, track, vote and record IN/OUT events.

Run one instance per camera from app/config.py CAMERAS. Each frame (about
PROCESS_FPS per second) goes through: detect -> ByteTrack -> quality filter ->
liveness -> embed the track's best frames -> match -> vote. Detection and tracking run on
a FRAME_WIDTH px copy; the faces are then scaled back to the camera's pixels,
so the quality filter (MIN_FACE_PX is camera pixels), the liveness crops, the ArcFace alignment
and the snapshot use the full-resolution frame. When a
track's vote is won, an event is written to data/attendance.db with a face
snapshot in data/snapshots/, unless the person is still in the cool-down for
this camera. Every match decision is logged (time, track, name or unknown,
score, second-best score) for threshold tuning.

Liveness (LIVENESS_ENABLED, app/liveness.py): a face that looks like a phone
screen or a printed photo (live score below LIVENESS_THRESHOLD) is skipped
("skip: spoof (score)"), never votes, is logged with a warning and gets one
review snapshot per track in data/snapshots/<day>/spoof/; a won vote is recorded
only after LIVENESS_MIN_FRAMES of the track's frames passed.

Unknown visitors (app/visitors.py), while the visitor log is on (Settings > System, read from the
database every UNKNOWN_SWITCH_SECONDS): a track whose kept frames all match nobody (below
UNKNOWN_MAX_SCORE) is recorded once, with a snapshot in data/snapshots/unknown/<day>/, while the person
is still in view; its last-seen time when the track ends; withdrawn if the track is recognised after all.
Their face embeddings stay in memory (UNKNOWN_MERGE_SECONDS), so a stranger who waits or comes back is one
visit. Never with --no-db.

The worker reports its status in data/run/<camera>.json and exits cleanly when
data/run/<camera>.stop appears (see app/workers.py), so the dashboard's
Cameras page can show and stop it whether it was started there or here. It
reloads data/gallery.npz when enrollment changes it. A second worker for the
same camera, or for a webcam another worker already uses, refuses to start.

Live sources (webcam, RTSP) are read on a background thread (app.camera.LiveCapture),
so a camera that hangs or that another app holds can't freeze the worker; it
warns when the camera sends no frames or only black ones, and opens the camera
again (with backoff) when it fails, never giving up.

Recording comes first: an error while processing one frame is logged, counted
(frame_errors in the status file) and that frame skipped; a live-view, status or
snapshot file that can't be written is skipped with a warning; an event the
database can't take right now is kept in memory and written when it can. A
gallery.npz that can't be read at the start is tried again every
GALLERY_RETRY_SECONDS. The annotated frame
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
import traceback
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app import config, db, dbconn, frame_source, logfile, sources, workers  # noqa: E402
from app.attendance import Cooldown  # noqa: E402
from app.camera import is_live, mask_source, open_capture, parse_source  # noqa: E402
from app.detector import (  # noqa: E402
    Detector, check_quality, detection_scale, quality_score, resize_to_width, scale_detection,
)
from app.fileutil import ErrorLog  # noqa: E402
from app.gallery import LOAD_ERRORS, Gallery, Match  # noqa: E402
from app.images import crop_face, write_image  # noqa: E402
from app.live import CameraHealth, LiveWriter  # noqa: E402
from app.liveness import SOURCE as LIVENESS_SOURCE, Liveness, LivenessUnavailable, context_crop  # noqa: E402
from app.models import device, onnx_threads, providers_in_use  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402
from app.tracker import FaceTracker, Sample, TrackState  # noqa: E402
from app.visitors import StrangerMemory, Visit, closest, qualifies, track_embedding  # noqa: E402

GREEN, RED, GRAY, YELLOW, ORANGE = (0, 200, 0), (0, 0, 255), (160, 160, 160), (0, 220, 220), (0, 140, 255)
FAILURES = {"no gallery", "could not open source", "video file not found", "lost the camera",
            "liveness model missing"}
STATS_SECONDS = 60  # log a summary of what the pipeline saw this often


def debug(line: str) -> None:
    """A per-frame line (a skipped face): logged only with --debug or WORKER_DEBUG_LOG. Without it, these are
    counted in the "stats:" line every STATS_SECONDS, so a person standing in view doesn't fill the log."""
    if config.WORKER_DEBUG_LOG:
        print(line)


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
    """Applies the cool-down, saves the snapshot and writes the event.

    Cool-down: one event per person per direction per COOLDOWN_SECONDS, whichever camera of that direction saw
    them (no reset at midnight, so a night shift isn't split). This worker's own memory (Cooldown) skips the obvious
    repeats cheaply; the database write (db.add_camera_event) checks again, in one transaction, against every
    camera's events.

    A snapshot that can't be saved never costs the event (it is recorded without one). An event the database
    can't take right now (locked for too long, disk full, SQL Server restarting) waits in `pending`, in order, and
    is written by the next flush() (each event and every housekeeping round); at most EVENT_QUEUE_MAX wait, the
    oldest go first. A lost SQL Server connection is replaced by the connection itself (app/mssql.py), at most
    every DB_RECONNECT_SECONDS; an event that failed once is looked for before it is written again (retry).
    """

    def __init__(self, cam: dict, source, use_db: bool):
        self.cam_name, self.direction = cam["name"], cam["direction"]
        self.conn = db.connect() if use_db else None
        last = {}
        if self.conn:
            self.camera_id = db.upsert_camera(self.conn, self.cam_name, source, self.direction)
            last = db.last_event_times(self.conn, datetime.now() - timedelta(seconds=config.COOLDOWN_SECONDS))
        self.cooldown = Cooldown(config.COOLDOWN_SECONDS, last)
        self.pending: deque[dict] = deque()
        self.db_errors = ErrorLog(f"{self.cam_name}: writing an event to the database (kept, will retry)")
        self.snapshot_errors = ErrorLog(f"{self.cam_name}: saving a face snapshot (event recorded without it)")

    def save_snapshot(self, now: datetime, name: str, image) -> str | None:
        """The snapshot's path relative to data/, or None if it couldn't be saved."""
        snap = config.SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / f"{now:%H%M%S}_{name}_{self.cam_name}.jpg"
        try:
            write_image(snap, image)
        except Exception as e:  # noqa: BLE001  disk full, folder locked, encoder error: keep the event anyway
            self.snapshot_errors(e)
            return None
        return snap.relative_to(config.DATA_DIR).as_posix()

    def save_spoof(self, now: datetime, track_id: int, score: float, image) -> str | None:
        """A spoof's review snapshot in data/snapshots/<day>/spoof/ (deleted with its day after
        SNAPSHOT_RETENTION_DAYS); its path relative to data/, or None (--no-db, or it couldn't be saved)."""
        if self.conn is None:
            return None
        snap = (config.SNAPSHOT_DIR / f"{now:%Y-%m-%d}" / "spoof"
                / f"{now:%H%M%S}_track{track_id}_{self.cam_name}_{score:.2f}.jpg")
        try:
            write_image(snap, image)
        except Exception as e:  # noqa: BLE001  only a review aid: never stop the camera for it
            self.snapshot_errors(e)
            return None
        return snap.relative_to(config.DATA_DIR).as_posix()

    def drop_snapshot(self, rel: str | None) -> None:
        if rel:
            try:
                (config.DATA_DIR / rel).unlink(missing_ok=True)
            except OSError as e:
                self.snapshot_errors(e)

    def flush(self) -> None:
        """Write the waiting events, oldest first; stops at the first database error (tried again later)."""
        while self.pending:
            ev = self.pending[0]
            event_id = previous = None
            try:
                person_id = db.active_person_id(self.conn, ev["code"])  # each time: dashboard changes apply live
                if person_id is not None:
                    event_id, previous = db.add_camera_event(self.conn, person_id, self.camera_id, ev["time"],
                                                             ev["score"], ev["snapshot"], config.COOLDOWN_SECONDS,
                                                             ev.get("retry", False))
            except dbconn.ERRORS as e:
                # Kept for the next flush. It may have been written before the error (SQL Server's connection lost
                # during COMMIT): the next try looks for it first, so it is never recorded twice.
                ev["retry"] = True
                self.db_errors(e)
                return
            self.pending.popleft()
            if person_id is None:
                ev["outcome"] = "inactive"
                print(f"{ev['head']}  NOT SAVED: inactive in the dashboard, or not enrolled (run scripts\\enroll.py)")
                continue
            if event_id is None:  # another camera of this direction recorded them a moment ago
                ev["outcome"], ev["previous"] = "cool-down", previous
                self.cooldown.seen(ev["code"], self.direction, previous)
                self.drop_snapshot(ev["snapshot"])
                print(f"{ev['head']}  ignored: cool-down ({config.COOLDOWN_SECONDS}s): {self.direction} already "
                      f"recorded at {previous:%H:%M:%S} (another camera)")
                continue
            ev["outcome"] = "saved"
            late = f" (waited since {ev['queued']:%H:%M:%S})" if ev.get("late") else ""
            print(f"{ev['head']}  EVENT saved{late}  score={ev['score']:.3f}  {ev['votes']}  "
                  f"snapshot={ev['snapshot'] or 'none'}")

    def record(self, stamp: str, now: datetime, track: TrackState, name: str, votes: int) -> tuple[str, str]:
        """Returns (decision kind, detail) for the live view: event, cool-down or not saved."""
        best = track.best_sample(name)
        head = f"{stamp}  track{track.track_id:<4} {self.direction:<3} {name}"
        if not self.cooldown.allow(name, self.direction, now):
            print(f"{head}  ignored: cool-down ({config.COOLDOWN_SECONDS}s) since last {self.direction}")
            return "cool-down", f"{self.direction} already recorded in the last {config.COOLDOWN_SECONDS} s"
        if self.conn is None:
            print(f"{head}  EVENT (not saved, --no-db)  score={best.match.score:.3f}  votes {votes}/{len(track.samples)}")
            return "not saved", "--no-db"
        ev = {"code": name, "time": now, "score": best.match.score, "head": head, "queued": datetime.now(),
              "votes": f"votes {votes}/{len(track.samples)}", "snapshot": self.save_snapshot(now, name, best.snapshot)}
        self.pending.append(ev)
        if len(self.pending) > config.EVENT_QUEUE_MAX:
            lost = self.pending.popleft()
            print(f"{lost['head']}  EVENT LOST: {config.EVENT_QUEUE_MAX} events were waiting for the database")
        self.flush()
        if ev.get("outcome") == "inactive":
            return "not saved", "person inactive or not enrolled"
        if ev.get("outcome") == "cool-down":
            return "cool-down", f"{self.direction} already recorded at {ev['previous']:%H:%M:%S} by another camera"
        if ev.get("outcome") is None:
            ev["late"] = True
            print(f"{head}  EVENT waiting for the database ({len(self.pending)} waiting); written as soon as it can be")
            return "event", f"{self.direction} waiting for the database ({ev['votes']})"
        return "event", f"{self.direction} saved ({ev['votes']})"


class VisitorLog:
    """Unknown visitors (app/visitors.py) seen by this camera, recorded while the visitor log is on (the switch in the
    database, read at most every UNKNOWN_SWITCH_SECONDS), each with one snapshot in data/snapshots/unknown/<day>/.

    A qualifying track is written at once, so the dashboard's alert comes while the person is still in view; its
    last-seen time and pass count when the track ends; a track recognised after all withdraws its visit (unless someone
    reviewed it meanwhile). A stranger seen again within UNKNOWN_MERGE_SECONDS (their embeddings are kept in memory
    only) adds a pass to the same visit. Writes the database can't take now wait in `pending`, in order (at most
    EVENT_QUEUE_MAX), like the Recorder's events; a visit not written yet when the log is switched off is dropped
    with its snapshot.
    """

    def __init__(self, recorder: Recorder, liveness: bool):
        self.conn, self.camera_id, self.cam_name = recorder.conn, recorder.camera_id, recorder.cam_name
        self.live_needed = config.LIVENESS_MIN_FRAMES if liveness else 0
        self.memory = StrangerMemory()
        self.open: dict[int, Visit] = {}  # track id -> its visit
        self.pending: deque[Visit] = deque()
        self._enabled: bool | None = None  # None until the switch was read once
        self._checked = float("-inf")
        self.errors = ErrorLog(f"{self.cam_name}: unknown visitors in the database (kept, will retry)")
        self.snapshot_errors = ErrorLog(f"{self.cam_name}: saving an unknown visitor's snapshot (recorded without it)")

    def enabled(self) -> bool:
        """The switch (a read that fails keeps the last answer: off until one succeeds)."""
        mono = time.monotonic()
        if mono - self._checked >= config.UNKNOWN_SWITCH_SECONDS:
            self._checked = mono
            try:
                on = db.unknown_log_enabled(self.conn)
            except dbconn.ERRORS as e:
                self.errors(e)
            else:
                if on != self._enabled:
                    print(f"{datetime.now():%H:%M:%S}  Unknown visitor log " + (
                        f"ON (Settings > System): a face is recorded once {config.UNKNOWN_MIN_FRAMES} kept frames "
                        f"all score below {config.UNKNOWN_MAX_SCORE}" if on else "OFF (Settings > System)"))
                self._enabled = on
        return bool(self._enabled)

    def check(self, stamp: str, now: datetime, track: TrackState, frame, full, live_score: float | None) -> str | None:
        """Record the track as an unknown visitor if it qualifies now (once per track); the live view's detail, or
        None."""
        tid = track.track_id
        if tid in self.open or not qualifies(track, config.UNKNOWN_MIN_FRAMES, config.UNKNOWN_MAX_SCORE,
                                             self.live_needed):
            return None
        if not self.enabled():
            return None
        embedding = track_embedding(track.samples)
        code, score = closest(track.samples)
        visit = self.memory.find(embedding, now)
        if visit is not None:  # the same stranger again (waited, or came back): one more pass of that visit
            visit.tracks += 1
            visit.last_seen = max(visit.last_seen, now)
            self.open[tid] = visit
            self.memory.remember(embedding, now, visit)
            self._queue(visit)
            self.flush()
            print(f"{stamp}  track{tid:<4} unknown visitor again: "
                  f"{f'visit #{visit.id}' if visit.id else 'the visit waiting for the database'}, pass {visit.tracks}")
            return f"same unknown visitor (pass {visit.tracks})"
        snapshot = self.save_snapshot(now, tid, context_crop(frame, full.bbox, size=config.UNKNOWN_SNAPSHOT_SIZE))
        visit = Visit(first_seen=now, last_seen=now, score=score, closest=code, liveness=live_score,
                      face_px=int(full.width), snapshot=snapshot)
        self.open[tid] = visit
        self.memory.remember(embedding, now, visit)
        self._queue(visit)
        self.flush()
        print(f"{stamp}  track{tid:<4} UNKNOWN VISITOR {f'#{visit.id}' if visit.id else '(waiting for the database)'}"
              f"  closest={fmt_score(code, score)}  frames {len(track.samples)}  snapshot={snapshot or 'none'}")
        return f"unknown visitor recorded (closest {code or '-'} {score:.2f})"

    def seen(self, track_id: int, now: datetime) -> None:
        """The track was in this frame: its visit's last-seen time (written when the track ends)."""
        visit = self.open.get(track_id)
        if visit is not None and now > visit.last_seen:
            visit.last_seen = now

    def close(self, track_id: int) -> None:
        """The track ended: its visit's last-seen time and passes are written."""
        visit = self.open.pop(track_id, None)
        if visit is not None and not visit.deleted:
            self._queue(visit)
            self.flush()

    def withdraw(self, stamp: str, track: TrackState, name: str) -> None:
        """The track was recognised after all: not a stranger. Its visit goes (or, merged with other passes, loses
        this one)."""
        visit = self.open.pop(track.track_id, None)
        if visit is None:
            return
        if visit.tracks > 1:
            visit.tracks -= 1
        else:
            visit.deleted = True
            self.memory.forget(visit)
        self._queue(visit)
        self.flush()
        print(f"{stamp}  track{track.track_id:<4} recognised as {name} after all: unknown visitor "
              + ("withdrawn" if visit.deleted else f"pass removed (visit #{visit.id})"))

    def save_snapshot(self, now: datetime, track_id: int, image) -> str | None:
        """The visitor's snapshot (the face with some background); its path relative to data/, or None."""
        snap = (config.SNAPSHOT_DIR / config.UNKNOWN_SNAPSHOT_FOLDER / f"{now:%Y-%m-%d}"
                / f"{now:%H%M%S-%f}_track{track_id}_{self.cam_name}.jpg")
        try:
            write_image(snap, image)
        except Exception as e:  # noqa: BLE001  a review aid: never stop the camera for it
            self.snapshot_errors(e)
            return None
        return snap.relative_to(config.DATA_DIR).as_posix()

    def drop_snapshot(self, rel: str | None) -> None:
        if rel:
            try:
                (config.DATA_DIR / rel).unlink(missing_ok=True)
            except OSError as e:
                self.snapshot_errors(e)

    def _queue(self, visit: Visit) -> None:
        if visit.queued:
            return  # its waiting write will use the latest values
        visit.queued = True
        self.pending.append(visit)
        if len(self.pending) > config.EVENT_QUEUE_MAX:
            lost = self.pending.popleft()
            lost.queued = False
            if lost.id is None:
                self.drop_snapshot(lost.snapshot)
            print(f"UNKNOWN VISITOR LOST: {config.EVENT_QUEUE_MAX} visitor writes were waiting for the database")

    def flush(self) -> None:
        """Write the waiting visits, oldest first; stops at the first database error (tried again later)."""
        while self.pending:
            visit = self.pending[0]
            try:
                if visit.deleted:
                    if visit.id is None or db.delete_unknown_visit(self.conn, visit.id):
                        self.drop_snapshot(visit.snapshot)  # kept if someone reviewed it meanwhile (the row stays)
                elif visit.id is None:
                    if self.enabled():
                        visit.id = db.add_unknown_visit(
                            self.conn, self.camera_id, visit.first_seen, visit.last_seen, visit.tracks, visit.score,
                            visit.closest, visit.liveness, visit.face_px, visit.snapshot, visit.retry)
                    else:  # switched off before it could be written: never written
                        visit.deleted = True
                        self.memory.forget(visit)
                        self.drop_snapshot(visit.snapshot)
                else:
                    db.update_unknown_visit(self.conn, visit.id, visit.last_seen, visit.tracks)
            except dbconn.ERRORS as e:
                if visit.id is None:
                    visit.retry = True  # it may have been written before the error: looked for first next time
                self.errors(e)
                return
            self.pending.popleft()
            visit.queued = False

    def housekeeping(self) -> None:
        """Every housekeeping round: the switch (so a change is logged at once) and the waiting writes."""
        self.enabled()
        if self.pending:
            self.flush()

    def finish(self) -> None:
        """At the worker's stop: every open visit's last-seen time written."""
        for track_id in list(self.open):
            self.close(track_id)
        if self.pending:
            print(f"{len(self.pending)} unknown visitor write(s) could not be saved before the stop")


class Pipeline:
    """Detect, track, check quality, embed, match, vote and record one frame; returns the annotated view.

    Detection, tracking and the view use the FRAME_WIDTH copy; the quality filter, embedding and snapshot use
    the full-resolution frame, with each face's box and landmarks scaled back to it.

    With `mirror`, only the returned view is flipped left-right (like a mirror or a video call); detection,
    recognition and snapshots use the original frame, the same orientation as the enrollment photos.

    `liveness` (app.liveness.Liveness, or None = no check): each quality-passing face of an undecided track is
    checked before it is embedded, while the track wants the frame or still needs live frames; a spoof is skipped.

    `visitors` (VisitorLog, or None = no visitor log, e.g. --no-db): an undecided track that qualifies as an unknown
    visitor is recorded once; a track recognised after all withdraws it; a track that ends writes its last-seen time.
    """

    def __init__(self, gallery: Gallery, detector, recognizer, recorder: Recorder, live: LiveWriter,
                 mirror: bool = False, liveness=None, visitors: VisitorLog | None = None):
        self.gallery, self.detector, self.recognizer = gallery, detector, recognizer
        self.recorder, self.live = recorder, live
        self.mirror = mirror
        self.liveness = liveness
        self.visitors = visitors
        self.tracker = FaceTracker(config.PROCESS_FPS)
        self.tracks: dict[int, TrackState] = {}
        self.times = deque(maxlen=20)
        self.liveness_ms = deque(maxlen=200)  # time of each liveness check (both models, one face)
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
            if self.visitors is not None:
                self.visitors.seen(tid, now)
            if track.decided:
                mark(det, GREEN, f"#{tid} {track.decided}")
                continue
            full = scale_detection(det, scale)  # the same face in the camera's pixels
            skip = check_quality(frame, full)
            if skip:
                debug(f"{stamp}  track{tid:<4} {'skip':<12} {skip}")
                self.stats[f"skip: {skip.split(' (')[0]}"] += 1
                self.live.decision("skip", tid, detail=skip, face_px=full.width)
                mark(det, GRAY, f"#{tid} skip: {skip.split(' (')[0]}")
                continue
            q = quality_score(full)
            wants = track.wants(q)
            live = None
            if self.liveness is not None and (wants or track.live_frames < config.LIVENESS_MIN_FRAMES):
                live = self.liveness.check(frame, full.bbox)
                self.liveness_ms.append(live.ms)
                if not live.live:
                    self.spoof(stamp, now, track, frame, full, live.score)
                    mark(det, ORANGE, f"#{tid} spoof {live.score:.2f}")
                    continue
                track.live_frames += 1
            if not wants:
                mark(det, YELLOW, f"#{tid} ...")
                self.decide(stamp, now, track)  # a live frame may be what a won vote was waiting for
                self.visitor(stamp, now, track, frame, full, live)  # ... or an unknown visitor's live frames
                continue
            embedding = self.recognizer.embed(frame, full)
            m = self.gallery.match(embedding)
            log_match(stamp, tid, m)
            track.add(Sample(q, m, crop_face(frame, full.bbox, config.SNAPSHOT_SIZE), embedding))
            score = None if live is None else live.score
            if m.name:
                self.stats["matched"] += 1
                votes = sum(s.match.name == m.name for s in track.samples)
                detail = f"vote {votes}/{config.VOTE_MIN_AGREE}"
                if self.liveness is not None:
                    detail += f", {track.liveness_summary(config.LIVENESS_MIN_FRAMES)}"
                self.live.decision("match", tid, m.name, m.score, m.second_score, detail, face_px=full.width,
                                   liveness=score)
            else:
                self.stats[f"unknown: {m.reason}"] += 1
                self.live.decision("unknown", tid, m.best_name, m.score, m.second_score, m.reason,
                                   face_px=full.width, liveness=score)
            mark(det, GREEN if m.name else RED, f"#{tid} {m.name or 'unknown'} {m.score:.2f}")
            self.decide(stamp, now, track)
            self.visitor(stamp, now, track, frame, full, live)

        for tid, track in list(self.tracks.items()):
            if t - track.last_seen > config.TRACK_LOST_SECONDS:
                if not track.decided and (track.samples or track.spoof_frames):
                    summary = track.votes_summary() or "no votes"
                    if self.liveness is not None:
                        summary += f"; {track.liveness_summary(config.LIVENESS_MIN_FRAMES)}"
                    print(f"{stamp}  track{tid:<4} closed as unknown (votes: {summary})")
                    self.live.decision("closed", tid, detail=f"left without a decision ({summary})")
                if self.visitors is not None:
                    self.visitors.close(tid)
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

    def decide(self, stamp: str, now: datetime, track: TrackState) -> None:
        """Record the track once one person won the vote and, with liveness on, enough of its frames were live."""
        won = track.vote()
        if not won:
            return
        need = config.LIVENESS_MIN_FRAMES if self.liveness is not None else 0
        if track.live_frames < need:
            if not track.waiting_logged:
                track.waiting_logged = True
                print(f"{stamp}  track{track.track_id:<4} {won[0]:<12} vote won, waiting for liveness "
                      f"({track.liveness_summary(need)})")
            return
        track.decided = won[0]
        if self.visitors is not None:
            self.visitors.withdraw(stamp, track, won[0])  # recorded as a visitor before the vote was won
        kind, detail = self.recorder.record(stamp, now, track, *won)
        self.stats[kind] += 1
        self.live.decision(kind, track.track_id, won[0], track.best_sample(won[0]).match.score, detail=detail)

    def visitor(self, stamp: str, now: datetime, track: TrackState, frame, full, live) -> None:
        """Record an undecided track that now qualifies as an unknown visitor (VisitorLog.check), once."""
        if self.visitors is None or track.decided:
            return
        detail = self.visitors.check(stamp, now, track, frame, full, None if live is None else live.score)
        if detail:
            self.stats["unknown visitor"] += 1
            self.live.decision("visitor", track.track_id, detail=detail, face_px=full.width,
                               liveness=None if live is None else live.score)

    def spoof(self, stamp: str, now: datetime, track: TrackState, frame, full, score: float) -> None:
        """A frame that looks like a phone screen or a printed photo: logged, shown, never embedded. The track's
        first spoof frame is saved for review (what the liveness model saw, with the background)."""
        tid = track.track_id
        track.spoof_frames += 1
        self.stats["skip: spoof"] += 1
        debug(f"{stamp}  track{tid:<4} {'skip':<12} spoof (live {score:.3f} < {config.LIVENESS_THRESHOLD})")
        if track.spoof_snapshot is None:
            track.spoof_snapshot = self.recorder.save_spoof(now, tid, score, context_crop(frame, full.bbox)) or ""
            print(f"{stamp}  WARNING: track{tid} looks like a phone screen or a printed photo (live score "
                  f"{score:.3f}, face {full.width}px); not recorded. Snapshot: {track.spoof_snapshot or 'not saved'}")
        self.live.decision("skip", tid, detail=f"spoof ({score:.2f})", face_px=full.width, liveness=score)

    def ms_per_frame(self) -> float:
        return 1000 * sum(self.times) / len(self.times) if self.times else 0.0

    def ms_per_liveness(self) -> float:
        return sum(self.liveness_ms) / len(self.liveness_ms) if self.liveness_ms else 0.0

    def stats_line(self) -> str:
        s = dict(self.stats)
        self.stats.clear()
        line = "stats: " + (", ".join(f"{k} {v}" for k, v in s.items()) if s else "no frames processed")
        if self.liveness_ms:
            line += f"; liveness {self.ms_per_liveness():.1f} ms/face"
        return line


def main() -> int:
    parser = argparse.ArgumentParser(description="GateSense attendance worker for one camera.")
    parser.add_argument("--camera", help="camera name from app/config.py CAMERAS (default: the first)")
    parser.add_argument("--source", help="override the camera's source: webcam index, video file or RTSP URL")
    parser.add_argument("--no-window", action="store_true", help="log only, don't show video")
    parser.add_argument("--no-db", action="store_true", help="don't write events or snapshots (for tuning)")
    parser.add_argument("--parent-pid", type=int, help="exit when this process ends (set by the dashboard)")
    parser.add_argument("--log-file", type=Path, help="write the log here, rotated by size (set by the dashboard)")
    parser.add_argument("--debug", action="store_true", help="also log every skipped frame (quality, spoof)")
    args = parser.parse_args()
    if args.log_file:
        logfile.redirect_output(args.log_file, config.WORKER_LOG_MAX_BYTES, config.WORKER_LOG_BACKUPS)
    if args.debug:
        config.WORKER_DEBUG_LOG = True
    try:
        config.check_cameras()
    except config.CamerasFileError as e:
        print(f"ERROR: {e}")
        return 1
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
    gallery, gallery_mtime = load_gallery(runtime)
    if gallery is None:
        return runtime.should_stop() or "stop requested"
    print(f"Camera {cam['name']} ({cam['direction']}), gallery: {gallery.counts()}")
    print(
        f"Threshold {config.MATCH_THRESHOLD}, margin {config.MATCH_MARGIN}, {config.PROCESS_FPS} fps, "
        f"best {config.BEST_FRAMES_PER_TRACK} frames, vote {config.VOTE_MIN_AGREE}, cool-down {config.COOLDOWN_SECONDS}s"
    )
    detector, recognizer = Detector(), Recognizer()
    print(f"Model {config.MODEL_PACK}, providers {providers_in_use()}, {onnx_threads()} ONNX threads "
          f"({'ONNX_THREADS' if config.ONNX_THREADS else 'automatic: CPU cores / cameras'})")
    checker = None
    if config.LIVENESS_ENABLED:
        try:
            checker = Liveness()  # downloads the models on first use, like InsightFace
        except LivenessUnavailable as e:  # on, but unavailable: never record without it (fail closed)
            print(f"ERROR: the liveness check is on (LIVENESS_ENABLED) but can't run: {e}")
            return "liveness model missing"
        print(f"Liveness on: live score >= {config.LIVENESS_THRESHOLD}, {config.LIVENESS_MIN_FRAMES} live frames per "
              f"track ({LIVENESS_SOURCE})")
    else:
        print("Liveness OFF (LIVENESS_ENABLED): phone screens and printed photos are not refused")
    dev = device()  # after every model loaded: a model that wouldn't load on the GPU moved them to the CPU
    print(f"Device {dev.label} (GATESENSE_DEVICE={dev.requested})"
          + (f", liveness providers {checker.providers()}" if checker else ""))
    if dev.warning:
        print(f"WARNING: {dev.warning}")
    runtime.device = dev.as_dict()
    mirror = bool(cam.get("mirror"))
    if mirror:
        print("View mirrored left-right (recognition uses the original frame)")
    recorder = Recorder(cam, source, use_db=not args.no_db)
    visitors = VisitorLog(recorder, checker is not None) if recorder.conn is not None else None
    if visitors is not None:
        visitors.enabled()  # logs whether the visitor log is on
    pipeline = Pipeline(gallery, detector, recognizer, recorder, live, mirror, checker, visitors)
    window = f"GateSense {cam['name']} ({cam['direction']})"
    label = f"{cam['name']} ({cam['direction']})"

    def housekeeping() -> str | None:
        """Status, stop request, gallery reload and periodic stats; returns a stop reason."""
        nonlocal gallery_mtime, last_gallery_check, last_stats
        runtime.tick()
        if stop := runtime.should_stop():
            return stop
        if pipeline.recorder.conn is not None and pipeline.recorder.pending:
            pipeline.recorder.flush()  # events the database couldn't take before
        if pipeline.visitors is not None:
            pipeline.visitors.housekeeping()  # the visitor log switch, and visits the database couldn't take
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
        if pipeline.visitors is not None:
            try:
                pipeline.visitors.finish()
            except Exception as e:  # noqa: BLE001  the stop goes on
                print(f"WARNING: unknown visitors at the stop: {type(e).__name__}: {e}")
        if pipeline.recorder.conn:
            pipeline.recorder.conn.close()
        if pipeline.times:
            print(f"Average processing time: {pipeline.ms_per_frame():.0f} ms/frame")
        if pipeline.liveness_ms:
            print(f"Average liveness check: {pipeline.ms_per_liveness():.1f} ms/face")


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
    frame_failed = FrameErrors(runtime)
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
            if capture.state == "reconnecting":  # the reconnect reason says more than "no frames"
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
            try:
                img = pipeline.process(frame, time.monotonic() - start, now, stamp)
                runtime.tick(processed=True)
                if show(img, stamp):
                    return "window closed"
            except Exception as e:  # noqa: BLE001  one bad frame must never stop the camera
                frame_failed(e)
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
    frame_failed = FrameErrors(runtime)
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
            try:
                img = pipeline.process(frame, t, now, stamp)
                runtime.tick(processed=True)
                if show(img, stamp):
                    return "window closed"
            except Exception as e:  # noqa: BLE001  one bad frame must never stop the camera
                frame_failed(e)
    finally:
        cap.release()


class FrameErrors:
    """An error while processing one frame: counted in the status file (frame_errors, last_frame_error), logged
    (the first with its traceback, then at most once a minute) and the frame skipped."""

    def __init__(self, runtime: "workers.WorkerRuntime"):
        self.runtime = runtime
        self.log = ErrorLog(f"{runtime.camera}: processing a frame (frame skipped)")

    def __call__(self, error: Exception) -> None:
        self.runtime.frame_errors += 1
        self.runtime.last_frame_error = f"{datetime.now():%H:%M:%S} {type(error).__name__}: {error}"
        try:
            if self.runtime.frame_errors == 1:
                print(traceback.format_exc(), end="")
            self.log(error)
        except OSError:  # the log itself can't be written (disk full): keep recording anyway
            pass


def load_gallery(runtime: "workers.WorkerRuntime") -> tuple[Gallery | None, float]:
    """The gallery and its mtime. A gallery.npz that can't be read (half-copied, damaged) is tried again every
    GALLERY_RETRY_SECONDS, with a warning on the Cameras page; (None, 0) if the worker is asked to stop meanwhile."""
    errors = ErrorLog("reading the face gallery (trying again)")
    while True:
        try:
            mtime = config.GALLERY_PATH.stat().st_mtime
            gallery = Gallery.load()
            runtime.warning = None
            return gallery, mtime
        except LOAD_ERRORS as e:
            errors(e)
            runtime.warning = f"the face gallery can't be read ({type(e).__name__}); trying again"
        deadline = time.monotonic() + config.GALLERY_RETRY_SECONDS
        while time.monotonic() < deadline:
            runtime.tick()
            if runtime.should_stop():
                return None, 0.0
            time.sleep(0.1)


def reload_gallery(gallery: Gallery, mtime: float) -> tuple[Gallery, float]:
    """Pick up enrollment changes made while running (the dashboard rewrites gallery.npz)."""
    try:
        new_mtime = config.GALLERY_PATH.stat().st_mtime
        if new_mtime == mtime:
            return gallery, mtime
        gallery = Gallery.load()
        print(f"Gallery reloaded: {gallery.counts()}")
        return gallery, new_mtime
    except LOAD_ERRORS as e:  # missing, mid-replace or damaged: keep the current one, try again next time
        _reload_errors(e)
        return gallery, mtime


_reload_errors = ErrorLog("reloading the face gallery (the one already loaded stays in use)")


if __name__ == "__main__":
    sys.exit(main())
