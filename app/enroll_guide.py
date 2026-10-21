"""Guided enrollment: steps (front, slightly left, slightly right), the live guide overlay, the
debounce before capture, and the review checks before saving. No Streamlit here, so it is testable
without a camera; dashboard/views/enrollment.py wires it to streamlit-webrtc and the page.

Yaw convention: InsightFace's yaw on a RAW (not mirrored) camera image is positive when the nose
points to the image's right. In a raw image the person's left is the image's right, so a positive
yaw means the person turned to THEIR left. All camera frames here are raw (WebRTC frames, OpenCV and
Streamlit's camera_input don't mirror); only the preview drawn by draw_overlay is mirrored, like a selfie.
"""

import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from app import config
from app.enroll_quality import QualityResult, check_photo

GREY, RED, GREEN = (225, 225, 225), (40, 40, 235), (60, 200, 60)
COLORS = {"none": GREY, "bad": RED, "good": GREEN}


# --- Steps and the sign convention ---------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    instruction: str
    arrow: str
    yaw: tuple[float, float]  # person's point of view: + = their left
    pitch: tuple[float, float]
    required: bool = True


def steps() -> list[Step]:
    return [Step(s["key"], s["title"], s["instruction"], s.get("arrow", ""), tuple(s["yaw"]), tuple(s["pitch"]),
                 bool(s.get("required", True))) for s in config.ENROLL_STEPS]


def person_yaw(image_yaw: float, mirrored: bool = False) -> float:
    """Yaw from the person's point of view (+ = turned to their own left) from InsightFace's yaw on the image.

    On a raw camera image the two are the same; on a mirrored (selfie) image the sign flips.
    """
    return -image_yaw if mirrored else image_yaw


def step_angle_hint(step: Step, yaw: float, pitch: float) -> str | None:
    """None when yaw/pitch fit the step, else a short instruction."""
    lo, hi = step.yaw
    if not lo <= yaw <= hi:
        if lo >= 0:  # a step turned to the person's left
            return "Turn a little more left" if yaw < lo else "Turn back a little"
        if hi <= 0:  # turned to their right
            return "Turn a little more right" if yaw > hi else "Turn back a little"
        return "Look straight at the camera"
    plo, phi = step.pitch
    if not plo <= pitch <= phi:
        return "Keep your head level"
    return None


def step_message(step: Step, yaw: float, pitch: float) -> str | None:
    """Plain sentence for a final check that is outside the step's range."""
    if step_angle_hint(step, yaw, pitch) is None:
        return None
    lo, hi = step.yaw
    side = "to your left" if yaw > 0 else "to your right" if yaw < 0 else "straight"
    if not lo <= yaw <= hi:
        return (f"For \"{step.title}\" the head must be at {lo:+.0f}° to {hi:+.0f}° (+ = your left); this photo is "
                f"{abs(yaw):.0f}° {side}. {step.instruction}")
    return f"Head tilted {abs(pitch):.0f}° up or down; keep it level (within {step.pitch[1]:.0f}°). {step.instruction}"


# --- Guide geometry and evaluation -------------------------------------------------------------------------


def guide_box(shape) -> tuple[int, int, int, int]:
    """The fixed box in the middle of the frame where the face should be (same in the mirrored view)."""
    h, w = shape[:2]
    bh = int(h * config.ENROLL_GUIDE_HEIGHT)
    bw = min(int(bh * config.ENROLL_GUIDE_ASPECT), w)
    x1, y1 = (w - bw) // 2, (h - bh) // 2
    return x1, y1, x1 + bw, y1 + bh


@dataclass
class GuideResult:
    state: str  # "none" (no face), "bad" (face, not acceptable) or "good"
    hint: str
    quality: QualityResult | None = None
    yaw: float | None = None  # person's point of view
    pitch: float | None = None

    @property
    def face_box(self):
        return None if self.quality is None or self.quality.detection is None else self.quality.detection.bbox


def evaluate(frame: np.ndarray, step: Step, detector) -> GuideResult:
    """What the guide shows for one frame: check_photo() plus position in the box and the step's angles."""
    q = check_photo(frame, detector)
    if q.code == "faces":
        return GuideResult("bad", "Only one person please", q)
    if q.detection is None:  # no face (or too dark to see one)
        return GuideResult("none", "Too dark" if q.code == "too_dark" else "Look at the camera", q)
    yaw, pitch = person_yaw(q.yaw), q.pitch
    x1, y1, x2, y2 = guide_box(frame.shape)
    bx1, by1, bx2, by2 = q.detection.bbox
    cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
    bad = lambda hint: GuideResult("bad", hint, q, yaw, pitch)  # noqa: E731
    if q.face_px < config.MIN_FACE_PX:
        return bad("Move closer")
    if q.face_px > (x2 - x1) * config.ENROLL_FACE_MAX_FRACTION:
        return bad("Move back a little")
    if not (x1 <= cx <= x2 and y1 <= cy <= y2) or q.code == "out_of_frame":
        return bad("Centre your face in the box")
    if q.brightness < config.ENROLL_MIN_BRIGHTNESS:
        return bad("Too dark")
    if hint := step_angle_hint(step, yaw, pitch):
        return bad(hint)
    if q.code == "blurry":
        return bad("Hold still")
    if not q.ok:
        return bad(q.reason.split(".")[0])
    return GuideResult("good", "Good, hold still", q, yaw, pitch)


def final_check(frame: np.ndarray, step: Step, detector) -> tuple[QualityResult, str | None]:
    """Check a captured full-resolution photo: every enrollment rule plus the step's angle range.

    Returns (quality, None) when it can be kept, or (quality, plain reason) when not.
    """
    q = check_photo(frame, detector)
    if not q.ok:
        return q, q.reason
    return q, step_message(step, person_yaw(q.yaw), q.pitch)


# --- Debounce ---------------------------------------------------------------------------------------------------


class Debouncer:
    """Green must hold without a break for `seconds` before it counts (stops flicker enabling Capture)."""

    def __init__(self, seconds: float | None = None, clock=time.monotonic):
        self.seconds = config.ENROLL_STABLE_SECONDS if seconds is None else seconds
        self.clock = clock
        self.since: float | None = None

    def update(self, good: bool) -> float:
        """Feed the latest state; returns how long it has been good without a break (0 if not good)."""
        now = self.clock()
        if not good:
            self.since = None
            return 0.0
        if self.since is None:
            self.since = now
        return now - self.since

    def held(self) -> float:
        return 0.0 if self.since is None else self.clock() - self.since

    @property
    def stable(self) -> bool:
        return self.since is not None and self.held() >= self.seconds

    def reset(self) -> None:
        self.since = None


# --- Overlay (pure) -------------------------------------------------------------------------------------------


def draw_overlay(frame: np.ndarray, result: GuideResult | None, step: Step | None = None,
                 mirror: bool = True, held: float = 0.0) -> np.ndarray:
    """New image: the frame (mirrored like a selfie) with the guide oval, corner lines, centre cross, face box
    and one hint line, coloured grey (no face), red (not acceptable) or green (acceptable). The input is untouched."""
    out = cv2.flip(frame, 1) if mirror else frame.copy()
    h, w = out.shape[:2]
    state = result.state if result else "none"
    color = COLORS[state]
    x1, y1, x2, y2 = guide_box(out.shape)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    t = max(2, w // 320)
    cv2.ellipse(out, (cx, cy), ((x2 - x1) // 2, (y2 - y1) // 2), 0, 0, 360, color, t, cv2.LINE_AA)
    arm = max(12, (x2 - x1) // 6)
    for (px, py, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(out, (px, py), (px + dx * arm, py), color, t + 1, cv2.LINE_AA)
        cv2.line(out, (px, py), (px, py + dy * arm), color, t + 1, cv2.LINE_AA)
    cross = max(6, w // 80)
    cv2.line(out, (cx - cross, cy), (cx + cross, cy), color, max(1, t - 1), cv2.LINE_AA)
    cv2.line(out, (cx, cy - cross), (cx, cy + cross), color, max(1, t - 1), cv2.LINE_AA)
    if result is not None and result.face_box is not None:
        bx1, by1, bx2, by2 = (int(v) for v in result.face_box)
        if mirror:
            bx1, bx2 = w - bx2, w - bx1
        cv2.rectangle(out, (bx1, by1), (bx2, by2), color, 1, cv2.LINE_AA)
    # Hint bar at the bottom, with a fill showing how long it has stayed green.
    bar = max(28, h // 14)
    cv2.rectangle(out, (0, h - bar), (w, h), (25, 25, 25), -1)
    if state == "good" and held > 0:
        frac = min(1.0, held / config.ENROLL_STABLE_SECONDS)
        cv2.rectangle(out, (0, h - 4), (int(w * frac), h), GREEN, -1)
    text = (result.hint if result else "Look at the camera")
    if step is not None:
        text = f"{step.title}: {text}"
    scale = bar / 40
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.putText(out, text, ((w - tw) // 2, h - (bar - th) // 2), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2,
                cv2.LINE_AA)
    return out


# --- Shared state between the video thread and the page ------------------------------------------------------


class LiveGuide:
    """Thread-safe state for one browser session's live guide.

    process() runs in streamlit-webrtc's video thread: it keeps the newest raw frame, hands it to a
    background analysis thread (at most ENROLL_ANALYSIS_FPS checks per second, so slow face checks never
    stall the video), and draws the overlay with the latest result. The page (script thread) only calls
    set_step(), snapshot() and take_frame(). Nothing here touches Streamlit.
    """

    def __init__(self, detector, step: Step, clock=time.monotonic, analysis_fps: float | None = None):
        self.detector = detector
        self.clock = clock
        self.interval = 1.0 / (analysis_fps or config.ENROLL_ANALYSIS_FPS)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._step = step
        self._frame: np.ndarray | None = None
        self._frame_time: float | None = None
        self._result: GuideResult | None = None
        self._debounce = Debouncer(clock=clock)
        self._video_times: list[float] = []
        self._analysis_times: list[float] = []
        self._thread: threading.Thread | None = None
        self.started = clock()

    # script thread -------------------------------------------------------------------------------------------
    def set_step(self, step: Step) -> None:
        with self._lock:
            if step.key != self._step.key:
                self._step, self._result = step, None
                self._debounce.reset()

    def snapshot(self) -> dict:
        with self._lock:
            now = self.clock()
            r = self._result
            return {
                "state": r.state if r else "none", "hint": r.hint if r else "Look at the camera",
                "held": self._debounce.held() if r and r.state == "good" else 0.0,
                "stable": bool(r and r.state == "good" and self._debounce.stable),
                "frames": len(self._video_times), "frame_age": None if self._frame_time is None else now - self._frame_time,
                "video_fps": _rate(self._video_times, now), "analysis_fps": _rate(self._analysis_times, now),
                "running_for": now - self.started,
            }

    def take_frame(self) -> np.ndarray | None:
        """Copy of the newest full-resolution raw frame, for the capture."""
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # video thread -------------------------------------------------------------------------------------------------
    def process(self, frame: np.ndarray) -> np.ndarray:
        with self._lock:
            now = self.clock()
            self._frame, self._frame_time = frame, now
            _push(self._video_times, now)
            result, step = self._result, self._step
            held = self._debounce.held() if result and result.state == "good" else 0.0
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._analyse, name="enroll-guide", daemon=True)
                self._thread.start()
        self._wake.set()
        return draw_overlay(frame, result, step, mirror=True, held=held)

    def analyse_now(self, frame: np.ndarray) -> GuideResult:
        """One analysis step (also used by the background thread and tests)."""
        with self._lock:
            step = self._step
        result = evaluate(frame, step, self.detector)
        with self._lock:
            if step.key == self._step.key:  # ignore a result for a step the page has moved past
                self._result = result
                self._debounce.update(result.state == "good")
                _push(self._analysis_times, self.clock())
        return result

    def _analyse(self) -> None:
        while True:
            if not self._wake.wait(timeout=10):
                return  # no frames for a while (camera stopped); process() restarts the thread
            self._wake.clear()
            t0 = self.clock()
            with self._lock:
                frame = self._frame
            if frame is not None:
                try:
                    self.analyse_now(frame)
                except Exception as e:  # noqa: BLE001  never let the analysis thread die silently
                    with self._lock:
                        self._result = GuideResult("bad", f"Check failed: {e}"[:60])
            spent = self.clock() - t0
            if spent < self.interval:
                time.sleep(self.interval - spent)


def _push(times: list[float], now: float, window: float = 2.0) -> None:
    times.append(now)
    while times and now - times[0] > window:
        times.pop(0)


def _rate(times: list[float], now: float) -> float:
    recent = [t for t in times if now - t <= 2.0]
    return 0.0 if len(recent) < 2 else (len(recent) - 1) / max(recent[-1] - recent[0], 1e-6)


# --- Captures, review and finish ---------------------------------------------------------------------------------


@dataclass
class Capture:
    step: str
    image: np.ndarray  # full-resolution raw frame
    quality: QualityResult
    embedding: np.ndarray


@dataclass
class GuidedSession:
    """One person's guided enrollment in progress (kept in st.session_state)."""

    code: str
    captures: dict[str, Capture] = field(default_factory=dict)

    @property
    def steps(self) -> list[Step]:
        return steps()

    @property
    def required(self) -> list[Step]:
        return [s for s in self.steps if s.required]

    def current(self) -> Step | None:
        """First required step without a photo, then optional ones; None when all are done."""
        for s in self.required + [s for s in self.steps if not s.required]:
            if s.key not in self.captures:
                return s
        return None

    def position(self) -> tuple[int, int]:
        """(1-based number of the current required step, number of required steps)."""
        done = sum(1 for s in self.required if s.key in self.captures)
        return min(done + 1, len(self.required)), len(self.required)

    @property
    def complete(self) -> bool:
        return all(s.key in self.captures for s in self.required)

    def accept(self, capture: Capture) -> None:
        self.captures[capture.step] = capture

    def retake(self, step_key: str) -> None:
        self.captures.pop(step_key, None)


def capture_photo(frame: np.ndarray, step: Step, detector, recognizer) -> tuple[Capture | None, str | None]:
    """Final check of a captured frame; (Capture, None) if kept, (None, plain reason) if not."""
    q, problem = final_check(frame, step, detector)
    if problem:
        return None, problem
    return Capture(step.key, frame, q, recognizer.embed(frame, q.detection)), None


def review(session: GuidedSession, gallery, threshold: float | None = None) -> dict[str, list[str]]:
    """Warnings per step: an outlier that doesn't match the other photos, or a photo closer to someone else."""
    threshold = config.ENROLL_SELF_MATCH_MIN if threshold is None else threshold
    caps = [c for c in session.captures.values()]
    warnings: dict[str, list[str]] = {c.step: [] for c in caps}
    if len(caps) >= 2:
        embs = np.stack([c.embedding / np.linalg.norm(c.embedding) for c in caps])
        sims = embs @ embs.T
        mean_to_others = [(sims[i].sum() - sims[i, i]) / (len(caps) - 1) for i in range(len(caps))]
        worst = int(np.argmin(mean_to_others))  # the outlier: least like the others
        lowest_pair = min(sims[worst, j] for j in range(len(caps)) if j != worst)
        if lowest_pair < threshold:
            warnings[caps[worst].step].append(
                f"Doesn't match the other photos well (similarity {lowest_pair:.2f}, needs {threshold:.2f}). "
                "It may be a bad photo or a different person: retake it.")
    if gallery is not None and len(gallery):
        for c in caps:
            scores = gallery.person_scores(c.embedding)
            ranked = sorted(((float(s), n) for s, n in zip(scores, gallery.names) if n != session.code), reverse=True)
            if ranked and ranked[0][0] >= config.MATCH_THRESHOLD:
                warnings[c.step].append(f"Looks similar to {ranked[0][1]} (similarity {ranked[0][0]:.2f}). "
                                        "Make sure this is the right person, then retake this photo.")
    return warnings


def finish(session: GuidedSession, detector, recognizer, conn):
    """Save the captured photos to known/<code>/ (with their step in photos.json) and rebuild the embeddings."""
    from app import enrollment

    if not session.complete:
        raise ValueError("take all the required photos first")
    extra = {}
    for step in session.steps:
        cap = session.captures.get(step.key)
        if cap is not None:
            path = enrollment.save_photo(session.code, cap.image)
            extra[path.name] = {"step": step.key}
    return enrollment.enroll_one(session.code, detector, recognizer, conn, extra=extra)
