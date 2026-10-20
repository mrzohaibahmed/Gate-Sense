import json
import shutil
from datetime import datetime

import cv2
import pytest

from app import config, db, enrollment
from app.images import write_image
from tests.fakes import FakeDetector, FakeRecognizer, photo

DET, REC = FakeDetector(), FakeRecognizer()


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


def add_files(code: str, **kinds) -> None:
    """add_files('ali', a='good', b='none') writes known/ali/a.png, b.png."""
    for i, (name, kind) in enumerate(kinds.items()):
        write_image(config.KNOWN_DIR / code / f"{name}.png", photo(kind, seed=i))


def counts():
    return enrollment.load_gallery().counts()


# --- Photo checks -----------------------------------------------------------------


@pytest.mark.parametrize("kind, reason", [
    ("good", None),
    ("none", "no face"),
    ("two", "2 faces"),
    ("small", "too small"),
    ("blurry", "blurry"),
])
def test_check_photo_reasons(kind, reason):
    check = enrollment.check_photo(photo(kind), DET, REC)
    if reason is None:
        assert check.accepted and check.embedding.shape == (512,)
    else:
        assert not check.accepted and check.reason.startswith(reason) and check.embedding is None


def test_unreadable_photo():
    assert enrollment.check_photo(None, DET, REC).reason == "unreadable image"
    assert enrollment.decode_image(b"not an image") is None


def test_enroll_folder_reports_every_photo():
    add_files("ali", good="good", noface="none", two="two", small="small", blurry="blurry")
    (config.KNOWN_DIR / "ali" / "broken.jpg").write_bytes(b"not an image")
    (config.KNOWN_DIR / "ali" / "notes.txt").write_text("ignored")
    r = enrollment.enroll_folder(config.KNOWN_DIR / "ali", DET, REC)
    assert r.accepted == ["good.png"] and len(r.embeddings) == 1
    reasons = dict(r.rejected)
    assert reasons["noface.png"] == "no face"
    assert reasons["two.png"] == "2 faces"
    assert reasons["small.png"].startswith("too small")
    assert reasons["blurry.png"].startswith("blurry")
    assert reasons["broken.jpg"] == "unreadable image"
    assert "notes.txt" not in reasons


# --- Shared enrollment (CLI full rebuild) ---------------------------------------------


def test_enroll_all_builds_gallery_and_persons(conn):
    add_files("ali", a="good", b="good")
    add_files("waqas", a="good", b="none")
    gallery, results, removed = enrollment.enroll_all(DET, REC, conn)
    assert counts() == {"ali": 2, "waqas": 1} and removed == []
    assert set(db.person_ids(conn)) == {"ali", "waqas"}
    assert {r.code: len(r.rejected) for r in results} == {"ali": 0, "waqas": 1}


def test_enroll_all_deactivates_removed_folders(conn):
    add_files("ali", a="good")
    add_files("waqas", a="good")
    enrollment.enroll_all(DET, REC, conn)
    shutil.rmtree(config.KNOWN_DIR / "ali")
    _, _, removed = enrollment.enroll_all(DET, REC, conn)
    assert removed == ["ali"] and counts() == {"waqas": 1}
    assert set(db.person_ids(conn)) == {"waqas"}


def test_enroll_all_keeps_dashboard_people_without_photos(conn):
    db.add_person(conn, "new1", "New Person", None, None, "added", "admin")
    add_files("waqas", a="good")
    enrollment.enroll_all(DET, REC, conn)
    assert set(db.person_ids(conn)) == {"new1", "waqas"}


# --- Dashboard operations ------------------------------------------------------------------


def test_add_photos_saves_only_accepted(conn):
    db.add_person(conn, "e1", "Ayesha", None, None, "added", "admin")
    results, person = enrollment.add_photos(
        "e1", [("a.jpg", photo("good", 1)), ("b.jpg", photo("none", 2)), ("c.jpg", photo("good", 3))], DET, REC, conn)
    assert [(label, check.reason, path.name if path else None) for label, check, path in results] == \
        [("a.jpg", None, "1.jpg"), ("b.jpg", "no face", None), ("c.jpg", None, "2.jpg")]
    assert sorted(p.name for p in (config.KNOWN_DIR / "e1").iterdir()) == ["1.jpg", "2.jpg"]
    assert person.accepted == ["1.jpg", "2.jpg"]  # saved JPEGs pass the same check again
    assert counts() == {"e1": 2}


def test_add_photos_nothing_accepted_changes_nothing(conn):
    results, person = enrollment.add_photos("e1", [("x", photo("blurry"))], DET, REC, conn)
    assert person is None and not (config.KNOWN_DIR / "e1").exists() and counts() == {}


def test_enroll_one_only_touches_that_person(conn):
    add_files("ali", a="good", b="good")
    enrollment.enroll_all(DET, REC, conn)
    enrollment.add_photos("e1", [("a", photo("good", 7))], DET, REC, conn)
    assert counts() == {"ali": 2, "e1": 1}


def test_large_upload_is_shrunk(conn):
    big = cv2.resize(photo("good"), (3200, 3200), interpolation=cv2.INTER_NEAREST)
    path = enrollment.save_photo("e1", big)
    assert max(cv2.imread(str(path)).shape[:2]) == config.ENROLL_MAX_PHOTO_PX


def test_delete_photo_rebuilds_embeddings(conn):
    enrollment.add_photos("e1", [("a", photo("good", 1)), ("b", photo("good", 2))], DET, REC, conn)
    person = enrollment.delete_photo("e1", "1.jpg", DET, REC, conn)
    assert person.accepted == ["2.jpg"] and counts() == {"e1": 1}
    for bad in ["../e1/2.jpg", "missing.jpg", "2.jpg/.."]:
        with pytest.raises(ValueError):
            enrollment.delete_photo("e1", bad, DET, REC, conn)


def test_delete_face_data_keeps_attendance(conn):
    db.add_person(conn, "e1", "Ayesha", None, None, "added", "admin")
    enrollment.add_photos("e1", [("a", photo("good", 1)), ("b", photo("good", 2))], DET, REC, conn)
    cam = db.upsert_camera(conn, "entrance", 0, "IN")
    db.add_event(conn, db.person_ids(conn)["e1"], cam, datetime(2026, 10, 20, 9, 0), 0.8, None)

    details = enrollment.delete_face_data("e1", "left the company", "admin", conn, deactivate=True)
    assert details == {"photos": 2, "embeddings": 2}
    assert not (config.KNOWN_DIR / "e1").exists() and counts() == {}
    assert conn.execute("SELECT COUNT(*) FROM attendance_events").fetchone()[0] == 1  # history kept
    assert db.person_ids(conn) == {}  # deactivated
    entry = db.audit_entries(conn)[0]
    assert entry["action"] == "delete_face_data" and entry["reason"] == "left the company"
    assert json.loads(entry["details"]) == {"photos": 2, "embeddings": 2, "deactivated": True}


def test_delete_face_data_needs_reason(conn):
    enrollment.add_photos("e1", [("a", photo("good"))], DET, REC, conn)
    with pytest.raises(ValueError):
        enrollment.delete_face_data("e1", "  ", "admin", conn, deactivate=False)
    assert (config.KNOWN_DIR / "e1" / "1.jpg").exists() and counts() == {"e1": 1}


def test_delete_face_data_can_keep_person_active(conn):
    db.add_person(conn, "e1", "Ayesha", None, None, "added", "admin")
    enrollment.add_photos("e1", [("a", photo("good"))], DET, REC, conn)
    enrollment.delete_face_data("e1", "retake photos", "admin", conn, deactivate=False)
    assert "e1" in db.person_ids(conn)


@pytest.mark.parametrize("code", ["e-01", "waqas", "A.B_c"])
def test_valid_codes(code):
    assert enrollment.validate_code(f" {code} ") == code


@pytest.mark.parametrize("code", ["", "..", ".hidden", "a/b", "a\\b", "a b", "CON", "nul.txt", "x" * 65])
def test_invalid_codes(code):
    with pytest.raises(ValueError):
        enrollment.validate_code(code)
