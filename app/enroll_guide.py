"""Guided enrollment: steps (front, slightly left, slightly right), the per-frame guide check, and the
review checks before saving. No web framework here, so it is testable without a camera;
api/routes/enrollment.py serves it to the web dashboard's enrollment wizard.

Yaw convention: InsightFace's yaw on a RAW (not mirrored) camera image is positive when the nose
points to the image's right. In a raw image the person's left is the image's right, so a positive
yaw means the person turned to THEIR left. All camera frames here are raw (the browser's camera frames
and OpenCV don't mirror); only the preview the person sees in the wizard is mirrored, like a selfie.
"""

from dataclasses import dataclass, field

import numpy as np

from app import config
from app.enroll_quality import QualityResult, check_photo

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
