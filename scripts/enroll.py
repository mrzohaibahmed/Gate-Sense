"""Build the face gallery from known/<employee code>/*.jpg.

Every photo is detected, quality-checked and embedded. Photos with no face,
more than one face, or a face that fails the quality filter are rejected.
All accepted embeddings are kept per person and saved to data/gallery.npz,
and the persons table in data/attendance.db is synced to match.
A summary per person lists accepted photos and why the others were rejected.
The same code (app/enrollment.py) backs the dashboard's Enrollment page.

Optionally capture photos from the webcam first:
    .venv\\Scripts\\python scripts\\enroll.py --capture waqas
(SPACE saves a photo when the face passes the quality filter, Q quits.)

Run from the project root:
    .venv\\Scripts\\python scripts\\enroll.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app import config, db, enrollment, workers  # noqa: E402
from app.camera import open_capture, parse_source  # noqa: E402
from app.detector import Detector, check_quality  # noqa: E402
from app.models import providers_in_use  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402

WINDOW = "GateSense enroll"


def capture(code: str, count: int, source, detector: Detector) -> None:
    """Save `count` webcam photos of one person into known/<code>/."""
    saved = 0
    cap = open_capture(source)
    print(f"Capturing {count} photos of {code!r}: SPACE = save, Q = quit")
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
                path = enrollment.save_photo(code, frame)
                print(f"  saved {path.relative_to(config.ROOT_DIR)}")
                saved += 1
    finally:
        cap.release()
        cv2.destroyAllWindows()
    print(f"Captured {saved} photo(s) of {code}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the GateSense face gallery from known/.")
    parser.add_argument("--capture", metavar="CODE", help="first capture photos of CODE from the webcam")
    parser.add_argument("--count", type=int, default=8, help="photos to capture (default 8)")
    parser.add_argument("--source", default=str(config.CAMERAS[0]["source"]), help="webcam index for --capture")
    args = parser.parse_args()

    if args.capture:
        try:
            args.capture = enrollment.validate_code(args.capture)
        except ValueError as e:
            print(f"ERROR: --capture: {e}")
            return 1
        source = parse_source(args.source)
        other = workers.webcam_conflict("", source, config.CAMERAS)
        if other:
            print(f"ERROR: webcam {source} is in use by the {other} worker; stop it first.")
            return 1

    detector, recognizer = Detector(), Recognizer()
    print(f"Model {config.MODEL_PACK}, providers {providers_in_use()}\n")

    if args.capture:
        capture(args.capture, args.count, parse_source(args.source), detector)

    conn = db.connect()
    gallery, results, removed = enrollment.enroll_all(detector, recognizer, conn)
    if not results:
        print(f"No person folders in {config.KNOWN_DIR}. Add photos as known/<code>/1.jpg, ... (see known/README.md)")
        conn.close()
        return 1

    for r in results:
        print(f"{r.code}: {len(r.accepted)} accepted, {len(r.rejected)} rejected")
        for photo, reason in r.rejected:
            print(f"    rejected {photo}: {reason}")
        if not r.accepted:
            print("    WARNING: no usable photos, this person is NOT enrolled")
        elif len(r.accepted) < config.ENROLL_MIN_PHOTOS:
            print(f"    WARNING: only {len(r.accepted)} photo(s); {config.ENROLL_MIN_PHOTOS} to 10 recommended")

    if not len(gallery):
        print("\nNo one enrolled; gallery not saved.")
        conn.close()
        return 1
    total = sum(gallery.counts().values())
    print(f"\nSaved {len(gallery)} people, {total} embeddings to {config.GALLERY_PATH.relative_to(config.ROOT_DIR)}")
    inactive = sorted(set(gallery.names) - db.person_ids(conn).keys())
    conn.close()
    print(f"Synced persons table in {config.DB_PATH.relative_to(config.ROOT_DIR)}")
    if removed:
        print(f"No longer enrolled, marked inactive: {', '.join(removed)}")
    if inactive:
        print(f"Note: {', '.join(inactive)} inactive in the dashboard; activate them on the People page to record attendance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
