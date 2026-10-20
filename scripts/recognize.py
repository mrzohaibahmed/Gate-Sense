"""Live recognition from a webcam, a video file or an RTSP stream.

Processes about PROCESS_FPS frames per second at ~FRAME_WIDTH px. Each face
that passes the quality filter is embedded and matched against the gallery,
and every decision is logged (time, name or unknown, score, second-best
score) so the threshold can be tuned. The largest face in view is also voted
on over its last few frames; a name that wins the vote prints a CONFIRMED line.

Run from the project root:
    .venv\\Scripts\\python scripts\\recognize.py                     # webcam from config
    .venv\\Scripts\\python scripts\\recognize.py --source 1          # another webcam
    .venv\\Scripts\\python scripts\\recognize.py --source clip.mp4   # recorded clip
Press Q or Esc in the window to quit.
"""

import argparse
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402

from app import config  # noqa: E402
from app.camera import is_webcam, open_capture, parse_source  # noqa: E402
from app.detector import Detector, check_quality, resize_to_width  # noqa: E402
from app.gallery import Gallery, Match  # noqa: E402
from app.models import providers_in_use  # noqa: E402
from app.recognizer import Recognizer  # noqa: E402

WINDOW = "GateSense"
GREEN, RED, GRAY = (0, 200, 0), (0, 0, 255), (160, 160, 160)
SKIPPED = object()  # largest face failed the quality filter, so it doesn't vote


def fmt_score(name: str | None, score: float | None) -> str:
    return "-" if score is None else f"{score:.3f} ({name})"


def log_match(stamp: str, i: int, m: Match) -> None:
    label = m.name if m.name else "unknown"
    verdict = "ACCEPT" if m.name else m.reason
    print(
        f"{stamp}  face{i}  {label:<12} score={fmt_score(m.best_name, m.score):<22} "
        f"second={fmt_score(m.second_name, m.second_score):<22} {verdict}"
    )


def draw(img, det, color, text) -> None:
    x1, y1, x2, y2 = det.bbox.astype(int)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
    cv2.putText(img, text, (x1, max(y1 - 8, 15)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description="GateSense live face recognition.")
    parser.add_argument(
        "--source",
        default=str(config.CAMERAS[0]["source"]),
        help="webcam index (0, 1, ...), video file path or RTSP URL",
    )
    parser.add_argument("--no-window", action="store_true", help="log only, don't show video")
    args = parser.parse_args()
    source = parse_source(args.source)

    if not config.GALLERY_PATH.exists():
        print(f"No gallery at {config.GALLERY_PATH}. Run scripts\\enroll.py first.")
        return 1
    gallery = Gallery.load()
    print(f"Gallery: {gallery.counts()}")
    print(f"Threshold {config.MATCH_THRESHOLD}, margin {config.MATCH_MARGIN}, {config.PROCESS_FPS} fps")

    detector, recognizer = Detector(), Recognizer()
    print(f"Model {config.MODEL_PACK}, providers {providers_in_use()}")

    try:
        cap = open_capture(source)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1
    webcam = is_webcam(source)
    # Files: process every Nth frame. Live sources: process at most PROCESS_FPS per second.
    src_fps = cap.get(cv2.CAP_PROP_FPS)
    if src_fps <= 0:  # webcams often report 0 or -1
        src_fps = 30.0
    step = 1 if webcam else max(1, round(src_fps / config.PROCESS_FPS))
    interval = 1.0 / config.PROCESS_FPS
    print(f"Source {source!r} at {src_fps:.0f} fps\n")

    votes: deque[str | None] = deque(maxlen=config.VOTE_FRAMES)
    confirmed = None
    last_face_t = None
    last_processed = -interval
    frame_idx = 0
    start = time.monotonic()
    times = deque(maxlen=20)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("End of video." if not webcam else "ERROR: lost the camera.")
                break
            frame_idx += 1

            if webcam:
                t = time.monotonic() - start
                if t - last_processed < interval:
                    continue
                stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            else:
                if (frame_idx - 1) % step:
                    continue
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                stamp = f"t={t:8.2f}s"
            last_processed = t

            t0 = time.perf_counter()
            small = resize_to_width(frame)
            dets = detector.detect(small)
            largest_decision = SKIPPED
            for i, det in enumerate(dets):
                reason = check_quality(small, det)
                if reason:
                    print(f"{stamp}  face{i}  {'skip':<12} {reason}")
                    draw(small, det, GRAY, "skip")
                    continue
                m = gallery.match(recognizer.embed(small, det))
                log_match(stamp, i, m)
                draw(small, det, GREEN if m.name else RED, f"{m.name or 'unknown'} {m.score:.2f}")
                if i == 0:
                    largest_decision = m.name
            times.append(time.perf_counter() - t0)

            # Vote over the largest face's recent decisions.
            if dets:
                last_face_t = t
                if largest_decision is not SKIPPED:
                    votes.append(largest_decision)
                    name, n = Counter(votes).most_common(1)[0]
                    if name and n >= config.VOTE_MIN_AGREE and name != confirmed:
                        confirmed = name
                        print(f"{stamp}  CONFIRMED {name} ({n}/{len(votes)} frames)")
            elif last_face_t is not None and t - last_face_t > config.FACE_LOST_SECONDS:
                votes.clear()
                confirmed = None
                last_face_t = None

            if not args.no_window:
                ms = 1000 * sum(times) / len(times)
                cv2.putText(small, f"{ms:.0f} ms/frame", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
                cv2.imshow(WINDOW, small)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()
    if times:
        print(f"Average processing time: {1000 * sum(times) / len(times):.0f} ms/frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
