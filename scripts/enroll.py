"""Build the face gallery from known/<person>/*.jpg.

Every photo is detected, quality-checked and embedded. Photos with no face,
more than one face, or a face that fails the quality filter are rejected.
All accepted embeddings are kept per person and saved to data/gallery.npz,
and the persons table in data/attendance.db is synced to match.
A summary per person lists accepted photos and why the others were rejected.

Optionally capture photos from the webcam first:
    .venv\\Scripts\\python scripts\\enroll.py --capture waqas
(SPACE saves a photo when the face passes the quality filter, Q quits.)

Run from the project root:
    .venv\\Scripts\\python scripts\\enroll.py
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app import config, db  # noqa: E402
from app.camera import open_capture, parse_source  # noqa: E402
from app.detector import Detector, check_quality  # noqa: E402
from app.gallery import Gallery  # noqa: E402
from app.images import read_image, write_image  # noqa: E402
from app.models import providers_in_use  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402

WINDOW = "GateSense enroll"


def capture(name: str, count: int, source, detector: Detector) -> None:
    """Save `count` webcam photos of one person into known/<name>/."""
    folder = config.KNOWN_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    existing = [int(p.stem) for p in folder.glob("*.jpg") if p.stem.isdigit()]
    next_num = max(existing, default=0) + 1
    saved = 0

    cap = open_capture(source)
    print(f"Capturing {count} photos of {name!r}: SPACE = save, Q = quit")
    print("Vary the pose a little: straight on, slightly left and right, with and without glasses.")
    try:
        while saved < count:
            ok, frame = cap.read()
            if not ok:
                print("ERROR: lost the camera")
                break
            dets = detector.detect(frame)
            if len(dets) == 1:
                reason = check_quality(frame, dets[0])
            else:
                reason = "no face" if not dets else f"{len(dets)} faces"

            view = frame.copy()
            for d in dets:
                x1, y1, x2, y2 = d.bbox.astype(int)
                cv2.rectangle(view, (x1, y1), (x2, y2), (0, 200, 0) if reason is None else (0, 0, 255), 2)
            status = f"{saved}/{count}  " + ("OK - press SPACE" if reason is None else reason)
            cv2.putText(view, status, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.imshow(WINDOW, view)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord(" "):
                if reason is not None:
                    print(f"  not saved: {reason}")
                    continue
                path = folder / f"{next_num}.jpg"
                write_image(path, frame)
                print(f"  saved {path.relative_to(config.ROOT_DIR)}")
                next_num += 1
                saved += 1
    finally:
        cap.release()
        cv2.destroyAllWindows()
    print(f"Captured {saved} photo(s) of {name}\n")


def enroll_person(folder: Path, detector: Detector, recognizer: Recognizer):
    """Return (embeddings, accepted photo names, [(photo name, reject reason)])."""
    embeddings, accepted, rejected = [], [], []
    photos = sorted(p for p in folder.iterdir() if p.suffix.lower() in config.IMAGE_EXTS)
    for photo in photos:
        img = read_image(photo)
        if img is None:
            rejected.append((photo.name, "unreadable image"))
            continue
        dets = detector.detect(img)
        if not dets:
            rejected.append((photo.name, "no face"))
            continue
        if len(dets) > 1:
            rejected.append((photo.name, f"{len(dets)} faces"))
            continue
        reason = check_quality(img, dets[0])
        if reason:
            rejected.append((photo.name, reason))
            continue
        embeddings.append(recognizer.embed(img, dets[0]))
        accepted.append(photo.name)
    return embeddings, accepted, rejected


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the GateSense face gallery from known/.")
    parser.add_argument("--capture", metavar="NAME", help="first capture photos of NAME from the webcam")
    parser.add_argument("--count", type=int, default=8, help="photos to capture (default 8)")
    parser.add_argument("--source", default=str(config.CAMERAS[0]["source"]), help="webcam index for --capture")
    args = parser.parse_args()

    if args.capture and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.capture):
        print("ERROR: --capture name may only use letters, digits, '_', '-' and '.'")
        return 1

    detector, recognizer = Detector(), Recognizer()
    print(f"Model {config.MODEL_PACK}, providers {providers_in_use()}\n")

    if args.capture:
        capture(args.capture, args.count, parse_source(args.source), detector)

    folders = sorted(p for p in config.KNOWN_DIR.iterdir() if p.is_dir()) if config.KNOWN_DIR.exists() else []
    if not folders:
        print(f"No person folders in {config.KNOWN_DIR}. Add photos as known/<name>/1.jpg, ... (see known/README.md)")
        return 1

    people = {}
    for folder in folders:
        embeddings, accepted, rejected = enroll_person(folder, detector, recognizer)
        people[folder.name] = embeddings
        print(f"{folder.name}: {len(accepted)} accepted, {len(rejected)} rejected")
        for photo, reason in rejected:
            print(f"    rejected {photo}: {reason}")
        if not accepted:
            print("    WARNING: no usable photos, this person is NOT enrolled")
        elif len(accepted) < config.ENROLL_MIN_PHOTOS:
            print(f"    WARNING: only {len(accepted)} photo(s); {config.ENROLL_MIN_PHOTOS} to 10 recommended")

    gallery = Gallery.from_people(people)
    if not len(gallery):
        print("\nNo one enrolled; gallery not saved.")
        return 1
    gallery.save()
    total = sum(gallery.counts().values())
    print(f"\nSaved {len(gallery)} people, {total} embeddings to {config.GALLERY_PATH.relative_to(config.ROOT_DIR)}")

    conn = db.connect()
    db.sync_persons(conn, gallery.names)
    conn.close()
    print(f"Synced persons table in {config.DB_PATH.relative_to(config.ROOT_DIR)} (people no longer enrolled are marked inactive)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
