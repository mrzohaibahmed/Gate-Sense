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
- Tracking: ByteTrack from **supervision** (MIT), pinned to **0.30.9**, the last release that includes it (0.31 moved it to Roboflow's separate `trackers` package). Not Ultralytics (AGPL).
- Per track: embed only the best `BEST_FRAMES_PER_TRACK` frames; record when one person wins `VOTE_MIN_AGREE` of them.
- One `recognize.py` process per camera (`--camera NAME` from `config.CAMERAS`); direction comes from the camera.
- Cool-down per (person, camera), seeded from the DB on start. For video files, event time = run start + video position.
- Storage: SQLite at `data/attendance.db` (WAL mode for several camera processes). Embeddings stay in `data/gallery.npz` for now. Day = calendar date (shift windows later).
- Dashboard: **Streamlit** (single Python app reading `data/attendance.db`), bound to **localhost**. FastAPI + React maybe later.
- Admin login: one password from the `GATESENSE_ADMIN_PASSWORD` environment variable; never hard-code it. No password set = dashboard refuses to run.
- Corrections never delete: add manual events, re-time, void/restore. Every change (events and people) needs a reason and writes `audit_log` in the same transaction.
- `enroll.py` adds new people and deactivates removed ones, but never re-activates someone an admin deactivated.
- Next phase: night-shift windows. Later: PostgreSQL, liveness check, Docker.

## Folder layout

```
app/
  config.py      thresholds, fps, frame size, model, camera sources
  models.py      loads the buffalo_s pack once (CPU providers only)
  detector.py    SCRFD face detection + quality filter
  recognizer.py  ArcFace embeddings
  gallery.py     every embedding per person; best-score cosine matching
  camera.py      opens a webcam index, video file or RTSP URL
  images.py      image read/write, face snapshot crops
  tracker.py     ByteTrack wrapper + per-track best frames and vote
  attendance.py  cool-down and daily summary rules (pure logic)
  db.py          SQLite schema, queries, corrections + audit log
  reports.py     daily/monthly report tables, CSV and Excel export
dashboard/
  main.py        Streamlit entry (login gate + navigation); not app.py, which would shadow the app package
  auth.py        admin login
  common.py      DB connection, snapshot thumbnails, flash messages
  views/         live, today, reports, people, corrections pages (not pages/, which Streamlit auto-loads)
scripts/
  check_env.py   verifies deps, model, webcam
  enroll.py      build data/gallery.npz from known/, sync persons table (optional --capture NAME)
  recognize.py   attendance worker for one camera (--camera, --source, --no-db)
  today.py       print a day's attendance (--date)
  dashboard.py   start the dashboard on DASHBOARD_HOST:DASHBOARD_PORT
known/<person>/*.jpg   enrollment photos (git-ignored)
data/                  gallery.npz, attendance.db, snapshots/ (all git-ignored)
tests/                 pytest tests
```

The InsightFace models download to `%USERPROFILE%\.insightface\models\buffalo_s` on first run.

## How to run

All commands from the project root, using the venv:

```
.venv\Scripts\python scripts\check_env.py                  # environment + webcam check
.venv\Scripts\python scripts\enroll.py                     # build gallery from known/
.venv\Scripts\python scripts\enroll.py --capture waqas     # capture webcam photos, then build
.venv\Scripts\python scripts\recognize.py                                  # entrance (webcam, IN)
.venv\Scripts\python scripts\recognize.py --camera exit --source clip.mp4  # exit (OUT) from a clip
.venv\Scripts\python scripts\recognize.py --no-db                          # tune without recording
.venv\Scripts\python scripts\today.py                                      # today's attendance
$env:GATESENSE_ADMIN_PASSWORD = "..."; .venv\Scripts\python scripts\dashboard.py   # dashboard
.venv\Scripts\python -m pytest                                             # tests (dashboard via AppTest)
```

`recognize.py` logs one line per match decision (time, track, name or unknown, score, second-best score, verdict) for threshold tuning, plus a line per event or cool-down skip.

## Rules

- **Build one phase at a time** and wait for the user's OK before starting the next.
- **Never commit face images or embeddings.** `known/`, `data/`, `*.npz` and image files are git-ignored; keep it that way.
- **Don't run `git commit`** at all; the user commits everything themselves.
- **Keep thresholds and camera settings in `app/config.py`.** Don't hard-code them in other modules.
