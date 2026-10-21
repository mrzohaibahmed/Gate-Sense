"""Enrollment photo rules (app/enroll_quality.py), the photos.json sidecar, and the messages on the page."""

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app import config, db, enroll_quality, enrollment
from app.detector import Detection, Detector
from app.enroll_quality import check_photo
from tests.fakes import FakeDetector, FakeRecognizer, face, photo


class PoseDetector(FakeDetector):
    """The fake detector plus a head pose, like the real Detector.pose (3D landmarks)."""

    def __init__(self, yaw=0.0, pitch=0.0):
        self.yaw, self.pitch, self.thresholds = yaw, pitch, []

    def detect(self, img, det_thresh=None):
        self.thresholds.append(det_thresh)
        return super().detect(img)

    def pose(self, img, det):
        return self.yaw, self.pitch, "3D landmarks"


class Faces:
    def __init__(self, n):
        self.n = n

    def detect(self, img, det_thresh=None):
        return [face(10 + 100 * i, 10, 90) for i in range(self.n)]


# --- Accepted -------------------------------------------------------------------------------------


def test_frontal_photo_is_accepted_with_all_numbers():
    det = PoseDetector(yaw=4, pitch=-3)
    q = check_photo(photo("good"), det)
    assert q.ok and q.code == "ok" and q.reason == "" and q.short == ""
    assert (q.yaw, q.pitch, q.face_px, q.det_score, q.pose_source) == (4, -3, 200, 0.9, "3D landmarks")
    assert 0 < q.score <= 100 and q.blur > config.MIN_BLUR_VAR and q.brightness > 100
    assert q.detection is not None


def test_enrollment_uses_its_own_detector_threshold():
    det = PoseDetector()
    check_photo(photo("good"), det)
    assert det.thresholds == [config.ENROLL_DET_THRESHOLD] == [0.4]


def test_real_detector_passes_the_threshold_through():
    calls = []

    class Model:
        def detect(self, img, **kw):
            calls.append(kw)
            return np.zeros((0, 5)), np.zeros((0, 5, 2))

    d = Detector(fa=SimpleNamespace(det_model=Model()))
    d.detect(photo("good"))
    d.detect(photo("good"), det_thresh=0.4)
    assert calls == [{}, {"det_thresh": 0.4}]  # live cameras: the model's own 0.5; enrollment: 0.4


# --- Pose limits ------------------------------------------------------------------------------------


@pytest.mark.parametrize("yaw", [52, -52])
def test_turned_face_has_a_plain_message(yaw):
    q = check_photo(photo("good"), PoseDetector(yaw=yaw))
    assert not q.ok and q.code == "turned"
    assert q.reason == "Face turned too far to the side (52°). Turn a little toward the camera (max 35°)."
    assert q.short == "turned sideways (52° > 35°)" and q.yaw == yaw


def test_yaw_and_pitch_limits_are_inclusive():
    assert check_photo(photo("good"), PoseDetector(yaw=35, pitch=-25)).ok
    assert check_photo(photo("good"), PoseDetector(yaw=35.5)).code == "turned"
    assert check_photo(photo("good"), PoseDetector(pitch=25.5)).code == "tilted"


def test_tilted_face_has_a_plain_message():
    q = check_photo(photo("good"), PoseDetector(pitch=-31))
    assert q.reason == "Head tilted too far up or down (31°). Look straight at the camera (max 25°)."


def test_limits_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "ENROLL_MAX_YAW", 60.0)
    assert check_photo(photo("good"), PoseDetector(yaw=52)).ok
    monkeypatch.setattr(config, "ENROLL_MAX_YAW", 20.0)
    assert "(max 20°)" in check_photo(photo("good"), PoseDetector(yaw=25)).reason


def test_pose_is_checked_before_blur():
    """A turned, blurry face is told to turn toward the camera (the actionable fix) rather than 'blurry'."""
    assert check_photo(photo("blurry"), PoseDetector(yaw=50)).code == "turned"


# --- Other refusals -------------------------------------------------------------------------------------


def test_no_face():
    q = check_photo(photo("none"), PoseDetector())
    assert q.code == "no_face" and q.reason == "No face found. Move closer and face the camera."


@pytest.mark.parametrize("n, text", [(2, "Two faces in the photo."), (3, "3 faces in the photo.")])
def test_several_faces(n, text):
    q = check_photo(photo("good"), Faces(n))
    assert q.code == "faces" and q.reason == f"{text} Only the person enrolling should be visible."
    assert q.short == f"{n} faces"


def test_face_too_small():
    q = check_photo(photo("small"), PoseDetector())
    assert q.code == "too_small" and q.reason == "Face too small (50 px). Move closer."


def test_blurry():
    q = check_photo(photo("blurry"), PoseDetector())
    assert q.code == "blurry" and q.reason.startswith("Photo is blurry. Hold still")


def test_too_dark_with_a_face():
    img = (photo("good").astype(np.float32) * 0.15).astype(np.uint8)
    img[0:20, 0:20] = 100  # the fake detector still "sees" one face
    q = check_photo(img, PoseDetector())
    assert q.code == "too_dark" and q.reason.startswith("Too dark (brightness ") and "Add light" in q.reason


def test_too_dark_without_a_face():
    q = check_photo(np.zeros((320, 320, 3), np.uint8), PoseDetector())
    assert q.code == "too_dark" and q.reason == "Too dark: no face could be seen. Add light in front of the person."


def test_unreadable():
    q = check_photo(None, PoseDetector())
    assert q.code == "unreadable" and q.reason == "Couldn't read this image. Use a JPG or PNG photo."


def test_every_refusal_has_a_plain_message():
    cases = [None, photo("none"), photo("small"), photo("blurry"), np.zeros((50, 50, 3), np.uint8)]
    for img in cases:
        q = check_photo(img, PoseDetector())
        assert not q.ok and q.reason and q.reason[0].isupper() and q.reason.endswith(".")
    for det in (PoseDetector(yaw=80), PoseDetector(pitch=80), Faces(2)):
        q = check_photo(photo("good"), det)
        assert not q.ok and q.reason.endswith(".")


# --- Score, fallback pose, JSON ----------------------------------------------------------------------------


def test_score_prefers_frontal_sharp_photos():
    frontal = check_photo(photo("good"), PoseDetector(yaw=0)).score
    turned = check_photo(photo("good"), PoseDetector(yaw=30)).score
    assert 0 <= turned < frontal <= 100


def test_landmark_fallback_when_no_3d_model():
    q = check_photo(photo("good"), FakeDetector())  # no pose() method
    assert q.ok and q.pose_source == "5 landmarks (approximate)" and abs(q.yaw) < 5 and abs(q.pitch) < 10


def test_landmark_fallback_sign_and_size():
    base = face(60, 60, 200)
    turned = Detection(base.bbox, base.kps.copy(), 0.9)
    turned.kps[2, 0] += 40  # nose well to one side
    yaw, _, _ = enroll_quality.landmark_pose(turned)
    assert yaw > 35
    turned.kps[2, 0] -= 80
    assert enroll_quality.landmark_pose(turned)[0] < -35


def test_to_json_has_no_detection_and_rounds():
    data = check_photo(photo("good"), PoseDetector(yaw=4.444)).to_json()
    assert "detection" not in data and data["yaw"] == 4.4 and data["ok"] is True
    json.dumps(data)


def test_live_matching_settings_unchanged():
    assert (config.MATCH_THRESHOLD, config.MATCH_MARGIN, config.DET_THRESHOLD) == (0.45, 0.05, 0.5)
    assert config.MAX_YAW_RATIO == 0.30  # the live cameras' own pose filter


# --- Through app.enrollment ----------------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


def test_enrollment_check_keeps_short_reason_and_adds_message():
    check = enrollment.check_photo(photo("good"), PoseDetector(yaw=50), FakeRecognizer())
    assert check.reason == "turned sideways (50° > 35°)"
    assert check.message.startswith("Face turned too far to the side (50°)")
    assert check.quality.yaw == 50 and check.embedding is None


def test_sidecar_written_for_saved_photos(conn):
    det = PoseDetector(yaw=12, pitch=-4)
    results, _ = enrollment.add_photos("e1", [("a", photo("good", 1)), ("b", photo("none", 2))], det,
                                       FakeRecognizer(), conn)
    folder = config.KNOWN_DIR / "e1"
    data = json.loads((folder / "photos.json").read_text())
    assert list(data["photos"]) == ["1.jpg"]  # the refused photo was never saved
    entry = data["photos"]["1.jpg"]
    assert entry["ok"] and entry["yaw"] == 12 and entry["pitch"] == -4 and 0 < entry["score"] <= 100
    assert data["limits"] == {"max_yaw": 35.0, "max_pitch": 25.0}
    assert enrollment.read_sidecar(folder) == data["photos"]
    assert [p.name for p in enrollment.photo_paths(folder)] == ["1.jpg"]  # photos.json isn't a photo


def test_sidecar_follows_deletes_and_full_rebuilds(conn):
    det = PoseDetector()
    enrollment.add_photos("e1", [("a", photo("good", 1)), ("b", photo("good", 2))], det, FakeRecognizer(), conn)
    enrollment.delete_photo("e1", "1.jpg", det, FakeRecognizer(), conn)
    assert list(enrollment.read_sidecar(config.KNOWN_DIR / "e1")) == ["2.jpg"]
    (config.KNOWN_DIR / "e2").mkdir()
    cv2.imwrite(str(config.KNOWN_DIR / "e2" / "1.png"), photo("blurry"))  # added by hand, for the CLI rebuild
    enrollment.enroll_all(det, FakeRecognizer(), conn)
    stored = enrollment.read_sidecar(config.KNOWN_DIR / "e2")
    assert stored["1.png"]["ok"] is False and stored["1.png"]["reason"].startswith("Photo is blurry")


def test_existing_photo_over_the_new_limit_is_reported_not_silently_dropped(conn):
    """A photo saved under the old rule but turned more than ENROLL_MAX_YAW is refused on the next rebuild,
    and photos.json says why, so the page shows it."""
    enrollment.add_photos("e1", [("a", photo("good", 1))], PoseDetector(yaw=20), FakeRecognizer(), conn)
    person = enrollment.enroll_one("e1", PoseDetector(yaw=37), FakeRecognizer(), conn)
    assert person.accepted == [] and person.rejected == [("1.jpg", "turned sideways (37° > 35°)")]
    assert enrollment.read_sidecar(config.KNOWN_DIR / "e1")["1.jpg"]["reason"].startswith("Face turned too far")


# --- On the page --------------------------------------------------------------------------------------------


def enrollment_page(monkeypatch, tmp_db, last=None):
    from tests.test_dashboard import logged_in

    at = logged_in(monkeypatch)
    at.session_state["enroll_person"] = "waqas"
    if last is not None:
        at.session_state["enroll_last_check"] = ("waqas", last)
    at.switch_page("views/enrollment.py").run()
    assert not at.exception
    return at


def test_page_shows_refusal_and_saved_result(monkeypatch, tmp_db):
    refused = {"ok": False, "reason": "Face turned too far to the side (52°). Turn a little toward the camera (max 35°)."}
    saved = {"ok": True, "reason": "", "score": 86.0, "yaw": 4.0, "pitch": -2.0}
    at = enrollment_page(monkeypatch, tmp_db, last=[("camera photo", None, refused, None),
                                                    ("b.jpg", None, saved, "1.jpg")])
    assert any(e.value == refused["reason"] for e in at.error)
    assert any(s.value == "Saved as 1.jpg. Score 86, yaw +4°, pitch -2°." for s in at.success)


def test_page_captions_come_from_photos_json(monkeypatch, tmp_db):
    folder = config.KNOWN_DIR / "waqas"
    folder.mkdir(parents=True)
    cv2.imwrite(str(folder / "1.jpg"), photo("good"))
    cv2.imwrite(str(folder / "2.jpg"), photo("good", 2))
    (folder / "photos.json").write_text(json.dumps({"photos": {
        "1.jpg": {"ok": True, "score": 86.0, "yaw": 4.0, "pitch": -2.0, "reason": ""},
        "2.jpg": {"ok": False, "reason": "Photo is blurry. Hold still and check that the camera is in focus."}}}))
    at = enrollment_page(monkeypatch, tmp_db)  # photos.json covers both photos: no model run needed
    captions = [i.caption for img in at.get("image") for i in img.proto.imgs]
    assert "✅ 1.jpg · score 86 · yaw +4°, pitch -2°" in captions
    assert "❌ 2.jpg: Photo is blurry. Hold still and check that the camera is in focus." in captions
    assert at.metric[1].value == "1"  # usable photos


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    conn = db.connect()
    db.sync_persons(conn, ["waqas"])
    yield conn
    conn.close()
