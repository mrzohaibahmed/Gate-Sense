"""Guided enrollment (app/enroll_guide.py): steps and angles, the yaw sign, the live guide's evaluation and
overlay, the debounce, captures, review, finish; and the page's Finish button. No camera needed."""

import json
import threading
import time

import numpy as np
import pytest

from app import config, db, enroll_guide, enroll_quality, enrollment
from app.detector import Detection
from app.enroll_guide import Debouncer, GuidedSession, LiveGuide, draw_overlay, evaluate, guide_box
from tests.fakes import FRONTAL_112, FakeRecognizer

STEPS = {s.key: s for s in enroll_guide.steps()}


def frame(w=640, h=480, level=None, seed=0):
    img = np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)
    return img if level is None else (img.astype(np.float32) * level / 128).clip(0, 255).astype(np.uint8)


class FaceAt:
    """Fake detector: one face (or `n`) of `size` px centred at (cx, cy), with a given pose (image yaw)."""

    def __init__(self, yaw=0.0, pitch=0.0, size=180, cx=320, cy=240, n=1):
        self.yaw, self.pitch, self.size, self.cx, self.cy, self.n = yaw, pitch, size, cx, cy, n

    def detect(self, img, det_thresh=None):
        out = []
        for i in range(self.n):
            x, y = self.cx - self.size / 2 + i * (self.size + 10), self.cy - self.size / 2
            out.append(Detection(np.array([x, y, x + self.size, y + self.size], np.float32),
                                 FRONTAL_112 * (self.size / 112) + [x, y], 0.9))
        return out

    def pose(self, img, det):
        return self.yaw, self.pitch, "3D landmarks"


class NoFace:
    def detect(self, img, det_thresh=None):
        return []


# --- Steps and the sign convention ---------------------------------------------------------------------


def test_steps_come_from_config():
    steps = enroll_guide.steps()
    assert [s.key for s in steps] == ["front", "left", "right"] and all(s.required for s in steps)
    assert STEPS["front"].yaw == (-10, 10) and STEPS["left"].yaw == (15, 35) and STEPS["right"].yaw == (-35, -15)
    assert all(s.pitch == (-20, 20) for s in steps)
    assert STEPS["left"].instruction == "Turn your head slightly to your left."


def test_optional_steps_can_be_added(monkeypatch):
    extra = {"key": "up", "title": "Slightly up", "instruction": "Lift your chin a little.", "yaw": (-10, 10),
             "pitch": (5, 20), "required": False}
    monkeypatch.setattr(config, "ENROLL_STEPS", config.ENROLL_STEPS + [extra])
    sess = GuidedSession("e1")
    assert [s.key for s in sess.required] == ["front", "left", "right"] and len(sess.steps) == 4


def test_yaw_sign_convention():
    """+ yaw = turned to the person's own left. On a raw camera image the person's left is the image's right,
    and InsightFace gives + yaw when the nose points to the image's right (measured on real photos: a face with
    the nose to the image's left read -36.6°). A mirrored (selfie) image flips it."""
    assert enroll_guide.person_yaw(25.0) == 25.0
    assert enroll_guide.person_yaw(25.0, mirrored=True) == -25.0
    # The 5-landmark fallback follows the same rule: nose shifted to the image's right -> + yaw -> "left" step.
    det = FaceAt(size=200).detect(None)[0]
    det.kps[2, 0] += 0.25 * (det.kps[1, 0] - det.kps[0, 0])  # nose toward the image's right
    yaw, _, _ = enroll_quality.landmark_pose(det)
    assert yaw > 15 and enroll_guide.step_angle_hint(STEPS["left"], enroll_guide.person_yaw(yaw), 0) is None


@pytest.mark.parametrize("step, yaw, pitch, hint", [
    ("front", 0, 0, None), ("front", 14, 0, "Look straight at the camera"), ("front", -14, 0, "Look straight at the camera"),
    ("left", 25, 0, None), ("left", 8, 0, "Turn a little more left"), ("left", -20, 0, "Turn a little more left"),
    ("left", 42, 0, "Turn back a little"),
    ("right", -25, 0, None), ("right", -8, 0, "Turn a little more right"), ("right", -42, 0, "Turn back a little"),
    ("front", 0, 24, "Keep your head level"), ("left", 20, -22, "Keep your head level"),
])
def test_step_angle_hints(step, yaw, pitch, hint):
    assert enroll_guide.step_angle_hint(STEPS[step], yaw, pitch) == hint


def test_step_range_edges_are_inclusive():
    assert enroll_guide.step_angle_hint(STEPS["left"], 15, 20) is None
    assert enroll_guide.step_angle_hint(STEPS["left"], 35, -20) is None
    assert enroll_guide.step_angle_hint(STEPS["front"], -10, 0) is None


# --- Live evaluation ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("detector, step, state, hint", [
    (NoFace(), "front", "none", "Look at the camera"),
    (FaceAt(n=2, size=150), "front", "bad", "Only one person please"),
    (FaceAt(size=60), "front", "bad", "Move closer"),
    (FaceAt(size=300), "front", "bad", "Move back a little"),
    (FaceAt(cx=110), "front", "bad", "Centre your face in the box"),
    (FaceAt(yaw=4), "front", "good", "Good, hold still"),
    (FaceAt(yaw=25), "left", "good", "Good, hold still"),
    (FaceAt(yaw=5), "left", "bad", "Turn a little more left"),
    (FaceAt(yaw=-25), "right", "good", "Good, hold still"),
    (FaceAt(yaw=25), "right", "bad", "Turn a little more right"),
    (FaceAt(pitch=23), "front", "bad", "Keep your head level"),
])
def test_evaluate(detector, step, state, hint):
    r = evaluate(frame(), STEPS[step], detector)
    assert (r.state, r.hint) == (state, hint)


def test_evaluate_dark_and_blurry():
    assert evaluate(frame(level=12), STEPS["front"], FaceAt()).hint == "Too dark"
    assert evaluate(np.zeros((480, 640, 3), np.uint8), STEPS["front"], NoFace()).hint == "Too dark"
    import cv2

    blurry = cv2.GaussianBlur(frame(), (41, 41), 15)
    assert evaluate(blurry, STEPS["front"], FaceAt()).hint == "Hold still"


def test_final_check_applies_the_step_range():
    q, problem = enroll_guide.final_check(frame(), STEPS["left"], FaceAt(yaw=25))
    assert problem is None and q.ok
    q, problem = enroll_guide.final_check(frame(), STEPS["left"], FaceAt(yaw=3))
    assert problem.startswith('For "Slightly left" the head must be at +15° to +35°') and "3° to your left" in problem
    q, problem = enroll_guide.final_check(frame(), STEPS["front"], FaceAt(yaw=50))
    assert problem.startswith("Face turned too far to the side (50°)")  # the general rule still applies


# --- Debounce ------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_debounce_needs_continuous_green():
    clock = Clock()
    d = Debouncer(0.7, clock)
    assert d.update(True) == 0 and not d.stable
    clock.t += 0.5
    assert d.update(True) == pytest.approx(0.5) and not d.stable
    clock.t += 0.1
    d.update(False)  # one red frame resets it
    clock.t += 0.4
    d.update(True)
    clock.t += 0.6
    assert not d.stable
    clock.t += 0.15
    assert d.stable and d.held() == pytest.approx(0.75)


def test_debounce_default_from_config():
    assert Debouncer().seconds == config.ENROLL_STABLE_SECONDS == 0.7


# --- Overlay (pure) ---------------------------------------------------------------------------------------------


def colour_pixels(img, bgr, tol=40):
    return int(np.all(np.abs(img.astype(int) - np.array(bgr)) <= tol, axis=2).sum())


def test_overlay_is_pure_and_mirrored():
    src = frame()
    before = src.copy()
    r = evaluate(src, STEPS["front"], FaceAt(yaw=2))
    out = draw_overlay(src, r, STEPS["front"])
    assert np.array_equal(src, before) and out is not src and out.shape == src.shape
    assert np.array_equal(out[5:40, 5:60], src[5:40, ::-1][:, 5:60])  # untouched corner = mirrored input
    plain = draw_overlay(src, r, STEPS["front"], mirror=False)
    assert np.array_equal(plain[5:40, 5:60], src[5:40, 5:60])


@pytest.mark.parametrize("detector, colour", [(NoFace(), enroll_guide.GREY), (FaceAt(cx=110), enroll_guide.RED),
                                             (FaceAt(), enroll_guide.GREEN)])
def test_overlay_colour_follows_state(detector, colour):
    blank = np.full((480, 640, 3), 90, np.uint8)  # plain grey background, so drawn colours stand out
    r = evaluate(frame(), STEPS["front"], detector)
    out = draw_overlay(blank, r, STEPS["front"])
    others = [c for c in (enroll_guide.GREY, enroll_guide.RED, enroll_guide.GREEN) if c != colour]
    assert colour_pixels(out, colour, 10) > 500
    assert all(colour_pixels(out[: 480 - 40], c, 10) < 50 for c in others)  # not counting the hint bar


@pytest.mark.parametrize("w, h", [(640, 480), (1280, 720), (640, 360)])
def test_guide_box_is_face_shaped_and_centred(w, h):
    x1, y1, x2, y2 = guide_box((h, w, 3))
    assert (x2 - x1) / (y2 - y1) == pytest.approx(config.ENROLL_GUIDE_ASPECT, abs=0.02)  # taller than wide
    assert abs((x1 + x2) / 2 - w / 2) <= 1 and abs((y1 + y2) / 2 - h / 2) <= 1


def test_overlay_draws_guide_box_and_hint_bar():
    blank = np.full((480, 640, 3), 90, np.uint8)
    out = draw_overlay(blank, None)
    x1, y1, x2, y2 = guide_box(blank.shape)
    assert (out[y1, x1:x1 + 20] != 90).any()  # corner guide line
    assert (out[460:, :] != 90).any()  # hint bar
    assert (out[240, 320] != 90).any()  # centre cross


def test_overlay_face_box_is_mirrored():
    blank = np.full((480, 640, 3), 90, np.uint8)
    r = evaluate(frame(), STEPS["front"], FaceAt(cx=250, size=100))
    out = draw_overlay(blank, r)
    assert (out[240, 640 - 200] != 90).any()  # box left edge 200 -> mirrored at 440
    assert (out[240, 200] == 90).all()


# --- Live guide (thread-safe state) ---------------------------------------------------------------------------------


def test_live_guide_snapshot_and_step_change():
    clock = Clock()
    g = LiveGuide(FaceAt(yaw=1), STEPS["front"], clock=clock)
    out = g.process(frame())
    assert out.shape == (480, 640, 3)
    g.analyse_now(frame())
    clock.t += 0.8
    g.analyse_now(frame())
    snap = g.snapshot()
    assert snap["state"] == "good" and snap["stable"] and snap["frames"] == 1
    assert np.array_equal(g.take_frame(), frame())
    g.set_step(STEPS["left"])  # a new step starts from scratch
    snap = g.snapshot()
    assert snap["state"] == "none" and not snap["stable"]


def test_live_guide_background_analysis_and_threads():
    g = LiveGuide(FaceAt(yaw=0), STEPS["front"], analysis_fps=20)
    stop = threading.Event()

    def video():
        while not stop.is_set():
            g.process(frame())
            time.sleep(0.01)

    t = threading.Thread(target=video)
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and g.snapshot()["state"] != "good":
        time.sleep(0.02)
    time.sleep(0.5)  # let a few frames arrive so the rate can be measured
    snaps = [g.snapshot() for _ in range(50)]  # read while the video thread writes
    stop.set()
    t.join()
    assert snaps[-1]["state"] == "good" and snaps[-1]["video_fps"] > 10


# --- Session, captures, review, finish ------------------------------------------------------------------------------


def capture(step, yaw, emb_seed=0):
    cap, problem = enroll_guide.capture_photo(frame(seed=emb_seed), STEPS[step], FaceAt(yaw=yaw), FakeRecognizer())
    assert problem is None
    return cap


def test_session_steps_and_finish_gate():
    sess = GuidedSession("e1")
    assert sess.current().key == "front" and sess.position() == (1, 3) and not sess.complete
    sess.accept(capture("front", 0))
    assert sess.current().key == "left" and sess.position() == (2, 3)
    sess.accept(capture("left", 25))
    assert not sess.complete
    sess.accept(capture("right", -25))
    assert sess.complete and sess.current() is None and sess.position() == (3, 3)
    sess.retake("left")
    assert not sess.complete and sess.current().key == "left"


def test_capture_refused_stays_on_step():
    cap, problem = enroll_guide.capture_photo(frame(), STEPS["left"], FaceAt(yaw=2), FakeRecognizer())
    assert cap is None and "Slightly left" in problem


def same_person(seed):
    base = np.random.default_rng(1).standard_normal(512)
    noise = np.random.default_rng(seed).standard_normal(512) * 0.3
    v = (base + noise).astype(np.float32)
    return v / np.linalg.norm(v)


def test_review_flags_outlier_and_lookalike():
    sess = GuidedSession("e1")
    for i, (step, yaw) in enumerate([("front", 0), ("left", 25), ("right", -25)]):
        cap = capture(step, yaw)
        cap.embedding = same_person(i)
        sess.accept(cap)
    assert enroll_guide.review(sess, None) == {"front": [], "left": [], "right": []}
    stranger = np.random.default_rng(99).standard_normal(512).astype(np.float32)
    sess.captures["right"].embedding = stranger / np.linalg.norm(stranger)
    warnings = enroll_guide.review(sess, None)
    assert warnings["front"] == [] and warnings["left"] == []
    assert warnings["right"][0].startswith("Doesn't match the other photos well")

    from app.gallery import Gallery

    gallery = Gallery.from_people({"EMP-012": [same_person(7)], "e1": [same_person(8)]})
    warnings = enroll_guide.review(sess, gallery)
    assert any(w.startswith("Looks similar to EMP-012") for w in warnings["front"])
    assert not any("e1" in w for ws in warnings.values() for w in ws)  # their own entry doesn't count


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


def test_finish_saves_photos_with_steps(conn):
    sess = GuidedSession("e1")
    with pytest.raises(ValueError):
        enroll_guide.finish(sess, FaceAt(), FakeRecognizer(), conn)
    for step, yaw in [("front", 0), ("left", 25), ("right", -25)]:
        sess.accept(capture(step, yaw))
    person = enroll_guide.finish(sess, FaceAt(), FakeRecognizer(), conn)
    assert len(person.accepted) == 3 and enrollment.load_gallery().counts() == {"e1": 3}
    stored = enrollment.read_sidecar(config.KNOWN_DIR / "e1")
    assert [(name, e["step"]) for name, e in stored.items()] == [("1.jpg", "front"), ("2.jpg", "left"),
                                                                 ("3.jpg", "right")]
    assert all("yaw" in e and "pitch" in e and "score" in e for e in stored.values())
    enrollment.enroll_one("e1", FaceAt(), FakeRecognizer(), conn)  # a later rebuild keeps the steps
    assert enrollment.read_sidecar(config.KNOWN_DIR / "e1")["2.jpg"]["step"] == "left"
    assert json.loads((config.KNOWN_DIR / "e1" / "photos.json").read_text())["photos"]["1.jpg"]["ok"]


# --- On the page: Finish is disabled until all 3 are taken --------------------------------------------------------


@pytest.fixture
def page_db(monkeypatch):
    c = db.connect()
    db.sync_persons(c, ["waqas"])
    yield c
    c.close()


def page(monkeypatch, sess):
    from tests.test_dashboard import logged_in

    at = logged_in(monkeypatch)
    at.session_state["enroll_person"] = "waqas"
    at.session_state["guided:waqas"] = sess
    at.switch_page("views/enrollment.py").run()
    assert not at.exception
    return at


def test_page_finish_disabled_until_three(monkeypatch, page_db):
    sess = GuidedSession("waqas")
    for step, yaw in [("front", 0), ("left", 25)]:
        sess.accept(capture(step, yaw))
    at = page(monkeypatch, sess)
    assert any(p.text == "Step 3 of 3: Slightly right" for p in at.get("progress") if hasattr(p, "text"))
    assert at.button(key="finish_waqas").disabled
    assert any("available when all 3 photos are taken (2 so far)" in c.value for c in at.caption)

    sess.accept(capture("right", -25))
    at = page(monkeypatch, sess)
    assert not at.button(key="finish_waqas").disabled
    assert any("All photos taken" in s.value for s in at.success)


def test_page_retake_reopens_the_step(monkeypatch, page_db):
    sess = GuidedSession("waqas")
    for step, yaw in [("front", 0), ("left", 25), ("right", -25)]:
        sess.accept(capture(step, yaw))
    at = page(monkeypatch, sess)
    at.button(key="retake_waqas_left").click().run()
    assert not at.exception and at.button(key="finish_waqas").disabled
    assert any(m.value.startswith("## ⬅️ Turn your head slightly to your left.") for m in at.markdown)
