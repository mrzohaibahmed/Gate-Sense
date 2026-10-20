# CLAUDE.md

**GateSense**: a face-recognition attendance system. An entrance camera records IN time and an exit camera records OUT time for enrolled (known) people only. Unknown faces are ignored.

**Read [PLAN.md](PLAN.md) first.** It is the full design. The decisions below override PLAN.md where they differ.

## Decisions

- Python 3.11. OpenCV for video: webcam now, RTSP IP cameras later.
- Face detection and recognition: InsightFace (SCRFD + ArcFace) with the **buffalo_s** model pack on **onnxruntime, CPU only**. No YOLO. GPU support is optional later.
- No model training. Enroll each person from 5 to 10 photos, turned into 512-d embeddings.
- Match by cosine similarity with a configurable threshold, starting at 0.45.
- CPU speed: process 5 to 10 fps, detect on frames about 640 px wide, recognize only the best few frames per person.
- Load only the `detection` and `recognition` modules (`allowed_modules`); buffalo_s also ships gender/age and landmark models we don't use.
- Later phases (not now): ByteTrack tracking, SQLite then PostgreSQL, FastAPI + dashboard, liveness check, Docker.

## Folder layout

```
app/
  config.py      thresholds, fps, frame size, model, camera sources
  models.py      loads the buffalo_s pack once (CPU providers only)
  detector.py    SCRFD face detection + quality filter
  recognizer.py  ArcFace embeddings
  gallery.py     every embedding per person; best-score cosine matching
  camera.py      opens a webcam index, video file or RTSP URL
scripts/
  check_env.py   verifies deps, model, webcam
  enroll.py      build data/gallery.npz from known/ (optional --capture NAME)
  recognize.py   live recognition, --source webcam index or video file
known/<person>/*.jpg   enrollment photos (git-ignored)
data/                  embeddings (gallery.npz) and database (git-ignored)
tests/                 pytest tests
```

The InsightFace models download to `%USERPROFILE%\.insightface\models\buffalo_s` on first run.

## How to run

All commands from the project root, using the venv:

```
.venv\Scripts\python scripts\check_env.py                  # environment + webcam check
.venv\Scripts\python scripts\enroll.py                     # build gallery from known/
.venv\Scripts\python scripts\enroll.py --capture waqas     # capture webcam photos, then build
.venv\Scripts\python scripts\recognize.py                  # live webcam
.venv\Scripts\python scripts\recognize.py --source clip.mp4 --no-window
.venv\Scripts\python -m pytest                             # tests
```

`recognize.py` logs one line per decision (time, name or unknown, score, second-best score, verdict) for threshold tuning.

## Rules

- **Build one phase at a time** and wait for the user's OK before starting the next.
- **Never commit face images or embeddings.** `known/`, `data/`, `*.npz` and image files are git-ignored; keep it that way.
- **Don't run `git commit`** at all; the user commits everything themselves.
- **Keep thresholds and camera settings in `app/config.py`.** Don't hard-code them in other modules.
