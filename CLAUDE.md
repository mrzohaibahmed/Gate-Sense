# CLAUDE.md

**GateSense**: a face-recognition attendance system. An entrance camera records IN time and an exit camera records OUT time for enrolled (known) people only. Unknown faces are ignored.

**Read [PLAN.md](PLAN.md) first.** It is the full design. The decisions below override PLAN.md where they differ.

## Decisions

- Python 3.11. OpenCV for video: webcam now, RTSP IP cameras later.
- Face detection and recognition: InsightFace (SCRFD + ArcFace) with the **buffalo_s** model pack on **onnxruntime, CPU only**. No YOLO. GPU support is optional later.
- No model training. Enroll each person from 5 to 10 photos, turned into 512-d embeddings.
- Match by cosine similarity with a configurable threshold, starting at 0.45.
- CPU speed: process 5 to 10 fps, detect on frames about 640 px wide, recognize only the best few frames per person.
- Full resolution after detection: the worker detects and tracks on the `FRAME_WIDTH` copy (`resize_to_width`), then `detector.scale_detection(det, detection_scale(frame, small))` moves box and landmarks to camera pixels; the quality filter (`MIN_FACE_PX` = camera px), ArcFace alignment and snapshot use the full frame. Tracking, voting, cool-down, the drawn view and the live JPEG stay at detection size. A frame already ≤ 640 px (webcam) isn't resized, so it's the same frame and detection objects as before. Live decisions carry `face_px` (camera px); the probe's median/warning are camera px too.
- Load only the `detection` and `recognition` modules (`allowed_modules`); buffalo_s also ships gender/age and landmark models we don't use.
- Tracking: ByteTrack from **supervision** (MIT), pinned to **0.30.9**, the last release that includes it (0.31 moved it to Roboflow's separate `trackers` package). Not Ultralytics (AGPL).
- Per track: embed only the best `BEST_FRAMES_PER_TRACK` frames; record when one person wins `VOTE_MIN_AGREE` of them.
- One `recognize.py` process per camera (`--camera NAME` from `config.CAMERAS`); direction comes from the camera.
- Cool-down per (person, camera), seeded from the DB on start. For video files, event time = run start + video position.
- Storage: SQLite at `data/attendance.db` (WAL mode for several camera processes). Embeddings stay in `data/gallery.npz` for now. Day = calendar date (shift windows later).
- Dashboard: **Streamlit** (single Python app reading `data/attendance.db`), bound to **localhost**. FastAPI + React maybe later.
- API (Phase 1, FastAPI): `api/` next to `app/` and the dashboard; `python -m api.run` / `start-api.bat` serve `api.main:create_app` (factory) on `API_HOST:API_PORT` (127.0.0.1:8000). CORS only for `API_CORS_ORIGINS` (`http://localhost:5173`, Vite), with credentials (the session cookie). `GET /api/health` (version, uptime, database status; 503 if the database can't be read) and `POST /api/auth/login` are the only public endpoints (`api.main.PUBLIC_PATHS`); a test calls every OpenAPI path without a cookie and expects 401. Pinned: fastapi 0.159.1, uvicorn[standard] 0.54.0, pydantic 2.14.1; tests need httpx2 2.16.0 (starlette 1.9's TestClient warns on plain httpx).
- API workers: `api.workers.WorkerManager` wraps `app.workers.WorkerManager` (same recognize.py processes and run-dir files as the dashboard); started in the lifespan (`open()` writes `data/run/_api.json`, a second API is refused) and `close()` stops the workers this API started, in parallel. `start()` raises `CameraBusy` when any process already runs the camera (says who: `held_by` = api / dashboard / command line / other, from `parent_pid` in the worker status file and `_dashboard.json`'s pids), when another worker has the same webcam, or while the dashboard's Enrollment camera is on (`dashboard/common.claim_camera` mirrors claims to `data/run/_enrollment_camera.json`). Backstop: recognize.py takes OS locks `camera-<name>.lock` and `webcam-<index>.lock` (`workers.acquire_camera`, msvcrt/fcntl; freed by the OS even on a kill) before opening the camera, so simultaneous starts can't both open it. Managers never probe the locks.
- API users (Phase 2): roles admin / hr / viewer / guard and their permissions only in `api/permissions.py` (`ROLE_PERMISSIONS`; some permissions are for later phases). Every endpoint uses `Depends(require_permission("x"))` from `api/auth.py`; denials are audited (`permission_denied`). argon2 (argon2-cffi 25.1.0) hashes; login sets cookie `SESSION_COOKIE` (httpOnly, SameSite=Strict, `Secure` only if `SESSION_COOKIE_SECURE`), token stored as SHA-256 in `sessions`; `SESSION_HOURS` 8 sliding (extended at most every `SESSION_REFRESH_SECONDS`, cookie re-sent). `LOGIN_MAX_FAILURES` 5 wrong passwords lock for `LOGIN_LOCK_MINUTES` 15 (right password refused meanwhile). First start with no users: `admin` from `GATESENSE_ADMIN_PASSWORD` (env or .env; refuses to start without it), `must_change_password` until changed; only `allow_password_change=True` endpoints (me, logout, password) work until then. Admin endpoints `/api/users` (list, create with temporary password, role, deactivate/activate, reset password) need a reason, audit in the same transaction, can't remove the last active admin or deactivate yourself. Audit actions: login, login_failed, login_locked, logout, permission_denied, change_password, password_change_failed, create_user, change_user_role, deactivate_user, activate_user, reset_user_password, start_camera, stop_camera; `user` = the username (typed name for failed logins). `api.auth.now` is the clock (tests replace it). The Streamlit dashboard still uses its single admin password.
- Schema versions: `app/db.py` `VERSIONS` (numbered steps, `PRAGMA user_version`, one `BEGIN IMMEDIATE` transaction, only adds). A database with data is copied to `<db folder>/backups/<name>-v<N>-<time>.db` first (before the write lock: SQLite's backup can't read through a connection holding it). Step 1: `users` (plus `must_change_password`, `failed_logins`, `locked_until`), `sessions`, `sites` (one default: `DEFAULT_SITE_NAME`, `DEFAULT_SITE_TIMEZONE` "local"), `cameras.site_id` (nullable for SQLite, filled with the default site; `upsert_camera`/`ensure_cameras` set it). `db.connect(path, migrate=False)` skips the checks (the API checks once per file, then one cheap connection per request). Camera start/stop endpoints: `GET /api/cameras` (cameras:read), `POST /api/cameras/{name}/start|stop` (cameras:control; busy = 409).
- API data endpoints (Phase 3): every endpoint has a pydantic response model; operation ids = function names (`generate_unique_id_function`), all unique, for the frontend's generated TypeScript. Lists return `Page[T]` (items, total, page, page_size) with `page`, `page_size` (max 500), `sort` (field, "-" = descending; unknown field = 422), `q` and, for rows that belong to a site, `site_id` (`api/paging.py`: in memory for people/cameras/attendance/users/sites, SQL for events and audit). Site scope: attendance, people and overview by the person's site (`persons.site_id`), events and cameras by the camera's site; users, sites and audit are global (no site_id). `/api/attendance` and `/api/overview` use `app.db.day_summaries` (pure; `refresh_daily`/the Today page use it too), so they match the Today page (verified in Edge on a copy of the real database). Corrections (`POST /api/attendance/corrections`, attendance:correct = hr, admin): add (person, direction or camera, time), change (keeps `original_timestamp` = the time before the first correction; the Streamlit Corrections page now keeps it too), void, restore; future times refused. Snapshots `/api/snapshots/{event_id}` and photos `/api/people/{id}/photos/{file}` only for logged-in users, paths confined to `data/` and the person's folder. Re-enroll uses `app.state.face_models` (lazy `Detector`, `Recognizer`; injectable). Cameras by DB id; the list is `cameras.json` (`camera_settings`), id/site from the cameras table; `public_source` keeps `${VAR}` placeholders and masks typed-in passwords; a Hikvision `password` goes to .env only; ROI (`camera_settings.check_roi`, fractions) is stored but not applied by the worker yet. `POST /api/cameras/test` refuses a running camera and a busy webcam; returns width, height, fps (reported), codec. Sites CRUD (sites:manage, admin; list sites:read, all roles); a site with cameras or people, or the last one, can't be deleted. Settings: `config.EDITABLE_SETTINGS` (thresholds, quality, vote, `COOLDOWN_SECONDS` = debounce, fps) with ranges; changes saved as overrides in `settings.json` (`config.SETTINGS_FILE`, `GATESENSE_SETTINGS_FILE`, git-ignored), applied at config import (`load_settings`, `refresh_settings`); workers read them at start; every change audited (`change_settings`, old/new). Tests reset settings to `DEFAULT_SETTINGS` (conftest). Schema step 2: `persons.site_id`, `attendance_events.original_timestamp`. `FrameSource` fills codec/fps from a connection that ended before being announced (very short files).
- API database: `api/db.py` `Database` is the API's only data access; backend from `config.DATABASE_URL` (env `GATESENSE_DATABASE_URL`; None = SQLite at `DB_PATH`), SQLite reuses `app.db`. `postgresql://` is recognised and refused ("not supported yet"); PostgreSQL needs a backend there plus porting `app.db` (the workers use it too).
- Admin login: one password from `GATESENSE_ADMIN_PASSWORD`, read from the environment or from `.env` in the project root (python-dotenv, `override=False`, so a real environment variable wins). Loaded by both `scripts/dashboard.py` and `dashboard/auth.py` via `app/env.py`. Never hard-code, print or log it; only say where it was found. No password in either = refuse to run. `.env` is git-ignored; `.env.example` is the template. Tests never read the real `.env` (see `tests/conftest.py`).
- Corrections never delete: add manual events, re-time, void/restore. Every change (events and people) needs a reason and writes `audit_log` in the same transaction.
- `enroll.py` adds new people and deactivates removed ones (folder gone from known/), but never re-activates someone an admin deactivated. People added on the dashboard with no photos yet are not deactivated.
- Enrollment photo rules all live in `app/enroll_quality.py` (`check_photo(image, detector) -> QualityResult`: ok, plain `reason`, short `short`, `code`, score 0-100, yaw/pitch in degrees, face_px, blur, det_score, brightness). Order: readable, face found (detector at `ENROLL_DET_THRESHOLD` 0.4), one face, size, in frame, brightness, yaw (`ENROLL_MAX_YAW` 35°), pitch (`ENROLL_MAX_PITCH` 25°), blur; pose before blur so turned faces get the actionable message. Pose comes from InsightFace's 3D-landmark model (`1k3d68.onnx`, `models.pose_model()`, `Detector.pose`), loaded only for enrollment; the 5-landmark estimate is a fallback only (it was off by up to 15° on real photos). The live cameras keep `detector.check_quality` (ratio-based) and the 0.45 / 0.05 match settings. Per-photo results are in `known/<code>/photos.json` (written by `enroll_one`/`enroll_all`, read by the Enrollment page so it needn't run models just to show photos).
- Guided enrollment: `app/enroll_guide.py` (no Streamlit: steps from `config.ENROLL_STEPS`, `evaluate`, `final_check`, `Debouncer`, pure `draw_overlay`, thread-safe `LiveGuide`, `GuidedSession`, `review`, `finish`) + `dashboard/guided_capture.py` (UI). Yaw convention: + = the person's own left; InsightFace yaw on a raw (unmirrored) image is + when the nose points to the image's right, which is the person's left; all frames (WebRTC, OpenCV, `st.camera_input`) are raw, only the drawn preview is mirrored. Live mode: streamlit-webrtc 0.79.0 (aiortc 1.15.0, av 17.1.0), `rtc_configuration={"iceServers": []}` (local only, no STUN), `async_processing=True`; the video callback only stores the frame and draws the overlay with the latest result; analysis runs on a background thread at most `ENROLL_ANALYSIS_FPS` times a second; the page polls `LiveGuide.snapshot()` from an `st.fragment(run_every=0.3)`. Never call Streamlit from the callback. WebRTC doesn't run under AppTest (needs the real runtime); `live_mode` catches that and falls back. Finish writes the step into `photos.json` (`write_sidecar(..., extra)`, `KEEP` fields survive rebuilds).
- Enrollment logic lives in `app/enrollment.py`, shared by `enroll.py` (full rebuild) and the dashboard's Enrollment page (per-person rebuild). Folder name = employee code. Delete face data removes photos + embeddings, keeps attendance, needs a reason, is audited.
- Workers: the dashboard's Cameras page runs `recognize.py --camera NAME --no-window --parent-pid <dashboard pid>` via `app/workers.py`. Protocol: status `data/run/<camera>.json`, clean stop via `data/run/<camera>.stop`, log `data/logs/<camera>.log`. Workers exit if the dashboard dies; a hard kill is only a fallback and only for workers this dashboard started. On Windows, launch the base interpreter with `__PYVENV_LAUNCHER__` (`workers.python_command`), as multiprocessing does, because the venv `python.exe` is a launcher with a child process. Same-webcam workers and duplicate workers per camera are refused (dashboard and CLI). Workers reload `gallery.npz` when it changes; the gallery is saved atomically.
- RTSP (`rtsp://`, e.g. Hikvision): `app/frame_source.py` `FrameSource`, chosen by `frame_source.open_live()`. FFmpeg backend, `OPENCV_FFMPEG_CAPTURE_OPTIONS` = `RTSP_FFMPEG_OPTIONS` (TCP, 5 s timeout; an existing env value wins), open/read timeouts 5000 ms. Newest frame only; reconnects forever with `RTSP_RECONNECT_BACKOFF` (1, 2, 4, 8, 16, 30 s; resets after a working connection) on open failure, read failure, no frame for `RTSP_STALE_SECONDS`, or no first frame within `RTSP_CONNECT_GRACE_SECONDS`. A stale connection is abandoned (its daemon thread releases the capture when the read returns), never joined. It never ends the worker. Health (state, size, input/processed fps, last frame time, reconnects today, last error, live since) goes into the worker status file (`health`) and the Cameras page badge. Mask passwords with `camera.mask_source` everywhere: logs, status, `cameras.source` in the DB, log tails, dashboard.
- Camera passwords never go in `app/config.py`: sources use `${VAR}` placeholders (`app/sources.py`), looked up in the environment, then `.env` (read fresh with `dotenv_values`, not loaded into `os.environ`). Values in the URL's `user:password@` part are percent-encoded (FFmpeg decodes them; verified). Expand only where a stream is opened (`recognize.run`, `probe_camera.main`); the dashboard, status, DB and logs keep the placeholder form. Missing/empty variable: `WorkerManager.start` refuses, the Cameras page shows the error, the worker exits with reason `missing environment variable X`. `GATESENSE_ENV_FILE` overrides the `.env` path (tests).
- Camera settings page (`app/camera_settings.py`, `dashboard/views/camera_settings.py`): cameras in `cameras.json` (`config.CAMERAS_FILE`, override `GATESENSE_CAMERAS_FILE`; replaces `DEFAULT_CAMERAS` when present; no passwords; git-ignored as site-specific, `cameras.example.json` is the committed template). The dashboard calls `config.refresh_cameras()` on every run so hand edits/deletion apply without a restart; with no file, `entries()` shows `DEFAULT_CAMERAS`. The camera fields are in an `st.form(enter_to_submit=False)`: outside a form, a password left with Tab (focus goes to the eye icon) was never sent to the app, which headless AppTest can't catch. Streamlit runs with `client.toolbarMode = "minimal"` (no Deploy button). Check UI changes in a real browser (Playwright driving the installed Edge from a scratch venv), not only AppTest. Types webcam / hikvision (URL built from IP, port, user, channel, main=01/sub=02) / rtsp (URL must use `${VAR}` for a password, typed passwords refused) / file. Hikvision passwords are written to `.env` as `GATESENSE_CAM_<NAME>_PASS` with python-dotenv `set_key` (other lines kept, verified by reading back) and never displayed. Running cameras can't be edited/deleted; names are fixed; changes audited via `db.log_change` (no passwords in details). `.env` is never loaded wholesale into the dashboard's environment (`env.load_env` copies only the admin password), otherwise workers would inherit stale camera passwords that win over `.env` edits.
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
  enrollment.py  enroll_all / enroll_one, add/delete photos, delete face data, photos.json sidecar
  enroll_quality.py  every enrollment photo rule + plain messages, head pose in degrees, score
  enroll_guide.py    guided capture: steps, live-guide evaluation + overlay, debounce, review, finish
  workers.py     worker runtime (status/stop files, parent watchdog) and WorkerManager
  live.py        live frame + decisions writer/reader, stale-frame message, camera health
  fileutil.py    atomic writes (temp file + os.replace with Windows retries)
api/
  main.py        FastAPI app factory create_app(workers, database): lifespan (tables, first admin, workers), CORS, health
  auth.py        argon2 passwords, login/lockout, sessions (cookie, sliding expiry), require_permission, first admin
  permissions.py roles and their permissions (the one place)
  paging.py      Page[T], list query parameters (page, page_size, sort, q, site_id), in-memory and SQL paging
  schemas.py     shared models (AttendanceRow, Event, Reason) and their builders
  routes/        auth, users, attendance (attendance, events, snapshots, corrections, overview), people, cameras,
                 sites, audit, settings
  workers.py     WorkerManager for the API: holder, CameraBusy, fps/last frame/last error, open/close
  db.py          Database: the API's only data access (users, sessions, audit); backend from config.DATABASE_URL
  run.py         `python -m api.run`: uvicorn on API_HOST:API_PORT (refuses if already running / port taken)
dashboard/
  main.py        Streamlit entry (login gate + navigation); not app.py, which would shadow the app package
  auth.py        admin login
  common.py      DB connection, snapshot thumbnails, flash messages, worker manager, Enrollment camera claims
  live_panel.py  one camera's live frame + decisions (Live view and Cameras pages)
  guided_capture.py  Enrollment page's guided photos: live WebRTC guide, simple capture, review, finish
  views/         live (events), live_view, today, reports, people, enrollment, cameras, corrections, settings
                 (not pages/, which Streamlit auto-loads)
scripts/
  check_env.py   verifies deps, model, webcam
  enroll.py      build data/gallery.npz from known/, sync persons table (optional --capture NAME)
  recognize.py   attendance worker for one camera (--camera, --source, --no-db)
  today.py       print a day's attendance (--date)
  dashboard.py   start the dashboard on DASHBOARD_HOST:DASHBOARD_PORT (--open, --preflight)
  stop.py        stop all camera workers, then the dashboard (stop.bat)
start.bat, stop.bat    Windows double-click start/stop (dashboard)
start-api.bat          Windows double-click start of the API (Ctrl+C stops it and its workers)
known/<person>/*.jpg   enrollment photos (git-ignored)
data/                  gallery.npz, attendance.db, snapshots/, run/, logs/, live/ (all git-ignored)
tests/                 pytest tests (fakes.py: fake detector/recognizer; fake_worker.py: fake recognize.py,
                       --frames = fake frame source; test_api.py / test_auth.py: FastAPI TestClient;
                       test_data_api.py: Phase 3 endpoints; test_migration.py: schema steps keep old data)
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
.venv\Scripts\python -m api.run                                            # API on 127.0.0.1:8000 (or start-api.bat)
.venv\Scripts\python scripts\dashboard.py                                  # dashboard (password from .env or env var)
.venv\Scripts\python -m pytest                                             # tests (dashboard via AppTest)
```

`recognize.py` logs one line per match decision (time, track, name or unknown, score, second-best score, verdict) for threshold tuning, plus a line per event or cool-down skip.

## Rules

- **Build one phase at a time** and wait for the user's OK before starting the next.
- **Never commit face images or embeddings.** `known/`, `data/`, `*.npz` and image files are git-ignored; keep it that way.
- **Don't run `git commit`** at all; the user commits everything themselves.
- **Keep thresholds and camera settings in `app/config.py`.** Don't hard-code them in other modules. Cameras are edited on the Camera settings page and saved to `cameras.json`, but `config.CAMERAS` (loaded by `config.load_cameras()`) stays the only place the rest of the code reads them from.
