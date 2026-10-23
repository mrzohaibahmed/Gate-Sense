# GateSense

Face-recognition attendance: an entrance camera records IN time and an exit camera records OUT time for enrolled people. Unknown faces are ignored. Runs on CPU with InsightFace (buffalo_s) and OpenCV. See [PLAN.md](PLAN.md) for the full design.

## Setup (Windows)

Requires Python 3.11 and a webcam.

```
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

No C++ Build Tools are needed; current insightface ships a prebuilt wheel.

Then set the first admin password: copy `.env.example` to `.env` and fill in `GATESENSE_ADMIN_PASSWORD=`. `.env` is git-ignored. A real `GATESENSE_ADMIN_PASSWORD` environment variable, if set, takes priority over `.env`.

```
copy .env.example .env
```

## Start and stop (Windows)

GateSense is one app: double-click **start.bat**. It checks everything first (and builds the web dashboard when it
changed, if Node.js is installed), starts GateSense in a minimized window and opens `http://localhost:8000` in the
browser. **stop.bat** stops the cameras cleanly, then the app. On the first start the account `admin` gets the
`GATESENSE_ADMIN_PASSWORD` from `.env` and must change it at the first login.

Other computers: set `GATESENSE_HOST=0.0.0.0` (and `GATESENSE_PORT` if 8000 is taken). Then GateSense requires
HTTPS: `GATESENSE_SSL_CERTFILE` and `GATESENSE_SSL_KEYFILE` (PEM files); `GATESENSE_ALLOW_HTTP=1` runs without it,
with a warning (passwords would cross the network unencrypted). To start at boot without anyone logged in, run
`scripts\install_service.ps1` from an administrator PowerShell (needs NSSM from https://nssm.cc).

Every night (after 02:00, while GateSense runs) the database and the face data are backed up to `data/backups/`
(kept 14 days; "Back up now" copies are kept until you delete them), and event snapshots older than 30 days are
deleted (the attendance itself is kept). Both are on Settings > System; the snapshot period is a setting.

After updating GateSense's code, restart it (`stop.bat`, then `start.bat`); start.bat rebuilds the web dashboard when it changed.

## Check the environment

```
.venv\Scripts\python scripts\check_env.py
```

On the first run this downloads the buffalo_s model (about 120 MB) to `%USERPROFILE%\.insightface\models`. It then opens the webcam, grabs one frame and prints how many faces it found and how long detection took.

## Enroll people

The easiest way is the dashboard's **Enrollment** page (see Dashboard below). From the command line instead, put 5 to 10 photos per person in `known/<employee code>/` (see [known/README.md](known/README.md)), then:

```
.venv\Scripts\python scripts\enroll.py
```

Or capture them from the webcam (SPACE saves when the face passes the quality check, Q quits):

```
.venv\Scripts\python scripts\enroll.py --capture waqas
```

It prints, per person, how many photos were accepted and why any were rejected, then saves `data/gallery.npz` and syncs the `persons` table in `data/attendance.db`.

## IP cameras (Hikvision and other RTSP cameras)

The easiest way is the dashboard's **Settings > Cameras**: **Add camera**, choose "Hikvision IP camera", fill in IP, user and password, press **Test connection**, then save. By hand instead: cameras live in `cameras.json` (written by that page) or, if it doesn't exist, in `DEFAULT_CAMERAS` in [app/config.py](app/config.py). Set the camera's `"source"` to its RTSP URL, with a `${VAR}` placeholder instead of the password (never write the password there). For Hikvision usually:

```
"source": "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101"   # 102 = sub stream
```

Then set the variable in `.env` in the project folder (git-ignored; see `.env.example`):

```
GATESENSE_CAM_PASS=your-camera-password
```

Write the password as is: `@`, `#`, `/`, `:` and other special characters are URL-encoded automatically when the stream is opened. If it contains spaces, `#` or quotes, put it in single quotes: `GATESENSE_CAM_PASS='p@ss #1'`. A real environment variable wins over `.env`; `start.bat` lists the ways to set one. Edits to `.env` apply the next time the camera starts. If a variable is missing, the Cameras page says which one and Start is refused.

RTSP cameras are read with FFmpeg over TCP with 5-second timeouts, always using the newest frame (no backlog, so recognition stays under a second behind). If the camera drops, stops sending frames for 5 seconds, or can't be reached, the worker keeps running and reconnects by itself after 1, 2, 4, 8, 16, then every 30 seconds, until you stop it. The Cameras page shows a green (live), amber (connecting/reconnecting) or red (offline) badge with resolution, input vs processed fps, last frame age, reconnects today and the last error. Camera passwords are masked in logs, the database and the dashboard.

### Probe a camera

Before relying on a camera, check its picture (stop that camera's worker first if it's a webcam):

```
.venv\Scripts\python -m app.probe_camera entrance                      # a camera name from CAMERAS
.venv\Scripts\python -m app.probe_camera "rtsp://user:pass@ip:554/Streaming/Channels/101"
.venv\Scripts\python -m app.probe_camera 0 --seconds 10                # webcam index or a video file
```

For 20 seconds (or `--seconds`) it prints the codec, resolution, input fps, time per frame, and the width and score of every face it detects. Walk past the camera while it runs. The summary gives the median face width in camera pixels (the worker detects on a 640 px copy, but checks quality and recognizes on the full-resolution frame) and warns "faces too small" if it is under 80 px. One snapshot with the face boxes is saved to `data/probe/<camera>_<time>.jpg` (git-ignored). Passwords are masked.

## Record attendance

Run one worker per camera, either with the Start buttons on the dashboard's **Live cameras** page or from the command line. Cameras (name, source, IN/OUT direction) are listed in `CAMERAS` in [app/config.py](app/config.py); `--source` overrides a camera's source. To test with one webcam, run two terminals:

```
.venv\Scripts\python scripts\recognize.py --camera entrance                       # webcam as IN
.venv\Scripts\python scripts\recognize.py --camera exit --source data\clips\out.mp4  # clip as OUT
```

Each face is tracked (ByteTrack); its best 5 frames are embedded, and when one person wins 3 of them an event is saved with a face snapshot in `data/snapshots/`. The same person on the same camera is ignored for the 5-minute cool-down. Add `--no-window` to log without video, or `--no-db` to tune without recording anything. Log lines look like:

```
13:28:34.814  track1    waqas        score=0.910 (waqas)          second=0.312 (ali)            ACCEPT
13:28:35.013  track2    unknown      score=0.402 (waqas)          second=0.298 (ali)            below threshold
13:28:35.140  track1    IN  waqas  EVENT saved  score=0.910  votes 3/3  snapshot=snapshots/2026-10-20/132835_waqas_entrance.jpg
13:31:02.517  track5    IN  waqas  ignored: cool-down (300s) since last IN
```

Use the scores to tune `MATCH_THRESHOLD` and `MATCH_MARGIN` in `app/config.py`.

## Today's attendance

```
.venv\Scripts\python scripts\today.py                    # today
.venv\Scripts\python scripts\today.py --date 2026-10-20
```

Prints name, first IN, last OUT, worked hours and a missing-exit flag for every enrolled person. Status is `present`, `missing exit` (no OUT after the last IN), `missing entry` (OUT without IN) or `absent`.

## Dashboard

GateSense's dashboard is a web app at `http://localhost:8000` (started by **start.bat**). Log in with your GateSense
account; the first one is `admin` with the `GATESENSE_ADMIN_PASSWORD` from `.env` (or the environment, which wins if
both are set), which must be changed at the first login. The password is never stored in the code or printed. Each
role (admin, HR, viewer, guard) sees only its own menu items.

- **Overview**: today's figures, arrivals per hour, camera health, live events and people needing attention.
- **Live cameras**: 1, 2, 4 or 9 cameras per screen with Start and Stop; full screen shows the camera's events and,
  for admins, its live recognition decisions (score, second-best, margin, face width in camera pixels, skip reason)
  for tuning the thresholds.
- **Attendance**: daily rows with filters; a side panel with the day's events and snapshots, corrections (add an IN or
  OUT, change a time) and void or restore an event (HR and admin; a reason is required, nothing is deleted, voided
  events are greyed and can be hidden).
- **People**: profile with photos, a month calendar and history; edit name, department, shift and site (the employee
  code can't change); delete one photo or all face data (photos and embeddings; attendance stays); deactivate.
- **Enrollment**: a guided wizard (front, slightly left, slightly right, then review and save). It asks **Which
  camera?** when IP cameras are set up: this PC's webcam (in the browser), or an IP camera such as the Hikvision at
  the gate, which GateSense reads itself at full resolution (its main stream) with a live preview and the same checks
  on the centre of the picture; if that camera is recording attendance, its frames are shared and attendance goes
  on. Each user's last choice is remembered. Or **Photo files**: drop up to 10 photos, each checked with the same rules as `enroll.py` (one face, at
  least 80 px wide, bright enough, turned at most 35°, tilted at most 25°, sharp), with a plain reason for every
  refused one. While the wizard has the webcam open, webcam camera workers can't start (and the wizard can't take
  the webcam from a running one); it gives the webcam back when it closes, or at the latest 90 seconds after the tab
  was closed.
- **Reports**: the daily register (first IN, last OUT, hours worked, missing OUT) and the monthly summary per person
  (days present, total hours, missing exits) for any date range and department, exported as CSV or Excel.
- **Audit log**: every change with who, when and why; CSV export.
- **Settings**: cameras (add, edit, test the connection, reorder, delete, view and download the log), sites, users and
  roles, recognition thresholds, and system information with "Back up now".

To work on the web dashboard itself, run the API (`start-api.bat`) and, in a second window:

```
cd web
npm install          (once)
npm run dev
```

then open `http://localhost:5173` (it talks to the API through the development server, cookies and the live
WebSocket included). Other commands: `npm test`, `npm run lint`, `npm run build`, `npm run gen:api` (refresh the API
types while the API runs).

## API (FastAPI, first phase)

The dashboard runs on a JSON API under `/api`, served by the same app (a Vite dev server on `http://localhost:5173` is the only other browser origin allowed). To run it in a window of its own:

```
start-api.bat                         (or: .venv\Scripts\python -m api.run)
```

It listens on `http://127.0.0.1:8000` (this PC only; `API_HOST` / `API_PORT` in `app/config.py`). `GET /api/health` returns the version, uptime and database status; interactive docs are at `/docs`. Press Ctrl+C in its window to stop it: camera workers it started are stopped first. A camera can only run in one process at a time: the API refuses a camera that the command line or the API already runs (or the webcam while the enrollment wizard holds it), and the worker itself locks the camera so two starts at the same moment can't both open it.

**Users and login.** The API has its own user accounts with four roles: **admin** (everything, including user accounts), **hr**, **viewer** and **guard** (the permissions of each are in `api/permissions.py`). On its first start the API creates the user `admin` with the `GATESENSE_ADMIN_PASSWORD`; that password must be changed at the first login. To try it, open `http://127.0.0.1:8000/docs`, run `POST /api/auth/login` with `{"username": "admin", "password": "..."}` (the page keeps the session cookie), then `POST /api/auth/password`, then the other endpoints. Admins manage accounts under `/api/users` (create with a temporary password, change role, deactivate, reset password; each change needs a reason). Sessions end after 8 hours without activity; 5 wrong passwords in a row lock an account for 15 minutes. Logins, failed logins, logouts and refused requests go to the audit log. On the first start after this update the database gets new tables (users, sessions, sites); a copy of the old database is saved in `data/backups/` first.

**Data endpoints** (used by the dashboard; all listed with their response types at `/docs`): `/api/overview` (the day at a glance: present, inside now, left, absent, missing exit, arrivals per hour, every camera's status), `/api/attendance` (daily rows, the same as the Overview), `/api/events` (raw IN/OUT events with score and snapshot; `/api/snapshots/{id}` shows a snapshot to logged-in users only), `/api/attendance/corrections` (HR and admin: add an IN/OUT, change a time, void or restore an event; the original time is kept and every correction needs a reason), `/api/people` (list, profile with photos and recent attendance, add, edit, deactivate, re-enroll), `/api/cameras` (list with status, add, edit, start, stop, and `/api/cameras/test` to open a camera once and see its resolution and fps), `/api/sites`, `/api/audit` and `/api/settings` (thresholds and the cool-down; admin only, each change audited, applied to cameras started afterwards). Lists take `page`, `page_size`, `sort` (e.g. `-date`), `q` (search) and, where it applies, `site_id`. Camera passwords never appear in a response: sources keep their `${VAR}` placeholders. A camera's region of interest can be saved, but the camera worker doesn't use it yet.

**Live video and live updates.** While logged in (admin, HR or guard), open `http://127.0.0.1:8000/api/cameras/1/stream` in the browser (or use it as an image source): the running camera's picture with face boxes and names, up to 8 frames a second and 960 px wide (`STREAM_FPS`, `STREAM_MAX_WIDTH` in `app/config.py`); `/api/cameras/1/snapshot.jpg` is one current frame. The camera encodes each frame once however many people watch, and goes back to its normal pace when nobody does. A web page can open the WebSocket `/api/ws` to get each new IN/OUT event, every camera's status every 2 seconds, and a notice after each correction.

## Tests

```
.venv\Scripts\python -m pytest
cd web && npm test
```

## Configuration

Thresholds, frame rate, frame size, tracking, vote and cool-down settings, and the camera list (name, source, IN/OUT direction) are in [app/config.py](app/config.py).

## Privacy

Face photos (`known/`), embeddings (`data/`, `*.npz`) and event snapshots (`data/snapshots/`) are biometric data. They are git-ignored and must never be committed. Enroll only people who have given written consent.
