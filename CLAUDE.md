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
- Admin login: one password from `GATESENSE_ADMIN_PASSWORD`, read from the environment or from `.env` in the project root (python-dotenv, `override=False`, so a real environment variable wins). Loaded by both `scripts/dashboard.py` and `dashboard/auth.py` via `app/env.py`. Never hard-code, print or log it; only say where it was found. No password in either = refuse to run. `.env` is git-ignored; `.env.example` is the template. Tests never read the real `.env` (see `tests/conftest.py`).
- Corrections never delete: add manual events, re-time, void/restore. Every change (events and people) needs a reason and writes `audit_log` in the same transaction.
- `enroll.py` adds new people and deactivates removed ones (folder gone from known/), but never re-activates someone an admin deactivated. People added on the dashboard with no photos yet are not deactivated.
- Enrollment logic lives in `app/enrollment.py`, shared by `enroll.py` (full rebuild) and the dashboard's Enrollment page (per-person rebuild). Folder name = employee code. Delete face data removes photos + embeddings, keeps attendance, needs a reason, is audited.
- Workers: the dashboard's Cameras page runs `recognize.py --camera NAME --no-window --parent-pid <dashboard pid>` via `app/workers.py`. Protocol: status `data/run/<camera>.json`, clean stop via `data/run/<camera>.stop`, log `data/logs/<camera>.log`. Workers exit if the dashboard dies; a hard kill is only a fallback and only for workers this dashboard started. On Windows, launch the base interpreter with `__PYVENV_LAUNCHER__` (`workers.python_command`), as multiprocessing does, because the venv `python.exe` is a launcher with a child process. Same-webcam workers and duplicate workers per camera are refused (dashboard and CLI). Workers reload `gallery.npz` when it changes; the gallery is saved atomically.
- RTSP (`rtsp://`, e.g. Hikvision): `app/frame_source.py` `FrameSource`, chosen by `frame_source.open_live()`. FFmpeg backend, `OPENCV_FFMPEG_CAPTURE_OPTIONS` = `RTSP_FFMPEG_OPTIONS` (TCP, 5 s timeout; an existing env value wins), open/read timeouts 5000 ms. Newest frame only; reconnects forever with `RTSP_RECONNECT_BACKOFF` (1, 2, 4, 8, 16, 30 s; resets after a working connection) on open failure, read failure, no frame for `RTSP_STALE_SECONDS`, or no first frame within `RTSP_CONNECT_GRACE_SECONDS`. A stale connection is abandoned (its daemon thread releases the capture when the read returns), never joined. It never ends the worker. Health (state, size, input/processed fps, last frame time, reconnects today, last error, live since) goes into the worker status file (`health`) and the Cameras page badge. Mask passwords with `camera.mask_source` everywhere: logs, status, `cameras.source` in the DB, log tails, dashboard.
- Camera passwords never go in `app/config.py`: sources use `${VAR}` placeholders (`app/sources.py`), looked up in the environment, then `.env` (read fresh with `dotenv_values`, not loaded into `os.environ`). Values in the URL's `user:password@` part are percent-encoded (FFmpeg decodes them; verified). Expand only where a stream is opened (`recognize.run`, `probe_camera.main`); the dashboard, status, DB and logs keep the placeholder form. Missing/empty variable: `WorkerManager.start` refuses, the Cameras page shows the error, the worker exits with reason `missing environment variable X`. `GATESENSE_ENV_FILE` overrides the `.env` path (tests).
- Camera settings page (`app/camera_settings.py`, `dashboard/views/camera_settings.py`): cameras in `cameras.json` (`config.CAMERAS_FILE`, override `GATESENSE_CAMERAS_FILE`; replaces `DEFAULT_CAMERAS` when present; no passwords; not git-ignored). Types webcam / hikvision (URL built from IP, port, user, channel, main=01/sub=02) / rtsp (URL must use `${VAR}` for a password, typed passwords refused) / file. Hikvision passwords are written to `.env` as `GATESENSE_CAM_<NAME>_PASS` with python-dotenv `set_key` (other lines kept, verified by reading back) and never displayed. Running cameras can't be edited/deleted; names are fixed; changes audited via `db.log_change` (no passwords in details). `.env` is never loaded wholesale into the dashboard's environment (`env.load_env` copies only the admin password), otherwise workers would inherit stale camera passwords that win over `.env` edits.
- Webcams (and other URLs) keep `camera.LiveCapture`; video files keep in-order reading. Don't route them through `FrameSource`.
- Live sources (webcam, RTSP) are read on a background thread (`camera.LiveCapture`, newest frame only) so a hung or busy camera never blocks status/Stop; `live.CameraHealth` warns on no frames or black frames (a webcam held by another app opens fine but gives 1 fps black frames, or blocks). Video files are still read in order on the main thread. `recognize.Pipeline.process()` is shared by both loops.
- Live view: workers write `data/live/<camera>.jpg` (the already-processed, annotated frame) and `<camera>.json` (fps, frame time, last 20 decisions) at most every `LIVE_FRAME_SECONDS`, atomically (`app/fileutil.py`); deleted on stop (the manager deletes them for a killed worker). No extra detection for the view.
- The Enrollment camera (`st.camera_input`) is opt-in via a "Use the camera" switch, because Streamlit renders all tabs and the browser would otherwise hold the webcam whenever the page is open. While it is on (`common.camera_claimed()`), webcam workers can't be started from the Cameras page.
- `start.bat` / `stop.bat` (CRLF, kept by `.gitattributes`): start.bat runs `scripts/dashboard.py --preflight` (0 ok, 1 can't start, 3 already running → opens browser), then the launcher with `--open` in a minimized window. The launcher writes `data/run/_dashboard.json` and, on `data/run/_dashboard.stop`, stops all workers then Streamlit. `stop.bat` runs `scripts/stop.py`: stop files to every live worker (any starter), then the dashboard stop file; it never kills. Use `ping` for delays in batch files, not `timeout` (fails without an interactive console).
- `GATESENSE_DATA_DIR` overrides `data/` (used for isolated runs). Tests never touch the real `known/`, `data/` or `.env` (`tests/conftest.py`).
- Next phase: night-shift windows. Later: PostgreSQL, liveness check, Docker.

## Folder layout

```
app/
  config.py      thresholds, fps, frame size, model, camera sources
  env.py         loads .env (never overrides real environment variables)
  sources.py     ${VAR} placeholders in camera sources: expand (URL-encodes passwords), missing_variables
  camera_settings.py  camera list editing: validate/build sources, cameras.json, .env passwords, test_connection
  models.py      loads the buffalo_s pack once (CPU providers only)
  detector.py    SCRFD face detection + quality filter
  recognizer.py  ArcFace embeddings
  gallery.py     every embedding per person; best-score cosine matching
  camera.py      opens a webcam index or video file; LiveCapture (webcam reader); mask_source
  frame_source.py  RTSP FrameSource (TCP, newest frame, reconnect with backoff, health, codec); open_live()
  probe_camera.py  `python -m app.probe_camera <camera|source>`: codec, resolution, fps, timing, face widths,
                   small-face warning (median < MIN_FACE_PX at FRAME_WIDTH), snapshot to data/probe/
  images.py      image read/write, face snapshot crops
  tracker.py     ByteTrack wrapper + per-track best frames and vote
  attendance.py  cool-down and daily summary rules (pure logic)
  db.py          SQLite schema, queries, corrections + audit log
  reports.py     daily/monthly report tables, CSV and Excel export
  enrollment.py  photo checks, enroll_all / enroll_one, add/delete photos, delete face data
  workers.py     worker runtime (status/stop files, parent watchdog) and WorkerManager
  live.py        live frame + decisions writer/reader, stale-frame message, camera health
  fileutil.py    atomic writes (temp file + os.replace with Windows retries)
dashboard/
  main.py        Streamlit entry (login gate + navigation); not app.py, which would shadow the app package
  auth.py        admin login
  common.py      DB connection, snapshot thumbnails, flash messages, worker manager, Enrollment camera claims
  live_panel.py  one camera's live frame + decisions (Live view and Cameras pages)
  views/         live (events), live_view, today, reports, people, enrollment, cameras, corrections, settings
                 (not pages/, which Streamlit auto-loads)
scripts/
  check_env.py   verifies deps, model, webcam
  enroll.py      build data/gallery.npz from known/, sync persons table (optional --capture NAME)
  recognize.py   attendance worker for one camera (--camera, --source, --no-db)
  today.py       print a day's attendance (--date)
  dashboard.py   start the dashboard on DASHBOARD_HOST:DASHBOARD_PORT (--open, --preflight)
  stop.py        stop all camera workers, then the dashboard (stop.bat)
start.bat, stop.bat    Windows double-click start/stop
known/<person>/*.jpg   enrollment photos (git-ignored)
data/                  gallery.npz, attendance.db, snapshots/, run/, logs/, live/ (all git-ignored)
tests/                 pytest tests (fakes.py: fake detector/recognizer; fake_worker.py: fake recognize.py)
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
.venv\Scripts\python -m app.probe_camera entrance                          # check a camera's picture (20 s)
start.bat / stop.bat                                                       # start / stop everything (double-click)
.venv\Scripts\python scripts\dashboard.py                                  # dashboard (password from .env or env var)
.venv\Scripts\python -m pytest                                             # tests (dashboard via AppTest)
```

`recognize.py` logs one line per match decision (time, track, name or unknown, score, second-best score, verdict) for threshold tuning, plus a line per event or cool-down skip.

## Rules

- **Build one phase at a time** and wait for the user's OK before starting the next.
- **Never commit face images or embeddings.** `known/`, `data/`, `*.npz` and image files are git-ignored; keep it that way.
- **Don't run `git commit`** at all; the user commits everything themselves.
- **Keep thresholds and camera settings in `app/config.py`.** Don't hard-code them in other modules. Cameras are edited on the Camera settings page and saved to `cameras.json`, but `config.CAMERAS` (loaded by `config.load_cameras()`) stays the only place the rest of the code reads them from.
