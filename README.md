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
browser. **stop.bat** stops the cameras cleanly, then the app (cameras with Auto on start again with the next start). On the first start the account `admin` gets the
`GATESENSE_ADMIN_PASSWORD` from `.env` and must change it at the first login.

Other computers: set `GATESENSE_HOST=0.0.0.0` (and `GATESENSE_PORT` if 8000 is taken). Then GateSense requires
HTTPS: `GATESENSE_SSL_CERTFILE` and `GATESENSE_SSL_KEYFILE` (PEM files); `GATESENSE_ALLOW_HTTP=1` runs without it,
with a warning (passwords would cross the network unencrypted). To start at boot without anyone logged in, run
`scripts\install_service.ps1` from an administrator PowerShell (needs NSSM from https://nssm.cc).

**Cameras that start by themselves.** Turn on **Auto** for a camera in Settings > Cameras: GateSense then starts it
whenever GateSense starts (after a reboot or a power cut, too, if GateSense itself starts at boot) and starts it
again whenever it stops: after 5 s, then waiting longer each time up to 5 minutes, back to 5 s once it has run for
5 minutes. A camera you stop with **Stop** on the Live cameras page (or stop.bat) stays stopped until you start it
again. Every automatic start is in the audit log (user `system`). A camera that can't be read for a moment is no
reason to stop: webcams and IP cameras are opened again by themselves, a frame that can't be processed is skipped
(counted in the worker's status file), and an event the database can't take right away is written as soon as it can.

GateSense refuses to start (start.bat says why) when `cameras.json` can't be read, rather than guessing which
cameras to use, or when the database is damaged (`PRAGMA quick_check`). Restore a backup then (below).

### Backups and restore

Every night (after 02:00, while GateSense runs), and with "Back up now" on Settings > System, GateSense makes a
backup set in `data/backups/`: the database (checked with `PRAGMA integrity_check`), the face data, the enrollment
photos (`known-….zip`), `cameras.json` and `settings.json`. **Never `.env`**: keep your own copy of it (it holds the
camera passwords), somewhere safe. The newest 14 nightly sets are kept (`BACKUP_KEEP`); "Back up now" sets are kept
until you delete them. Event snapshots older than 30 days are deleted (the attendance itself is kept; the period is a
setting).

Keep a second copy off this disk: set `GATESENSE_BACKUP_COPY_DIR` in `.env` to a folder on another disk or a USB
drive (e.g. `GATESENSE_BACKUP_COPY_DIR=E:\GateSense-backups`) and restart GateSense. Every set is then copied there
too and checked; the newest 14 nightly sets are kept there as well. If the copy fails (the drive is unplugged),
Settings > System shows the nightly backup as failed with the reason; the set in `data/backups/` is still fine.

To restore (GateSense is stopped for you, and started again by you afterwards):

```
.venv\Scripts\python scripts\restore.py --list                         # the backup sets, newest first
.venv\Scripts\python scripts\restore.py latest                         # restore the newest set
.venv\Scripts\python scripts\restore.py 20261023-020005                # or one set, by its time from --list
.venv\Scripts\python scripts\restore.py latest --from E:\GateSense-backups   # from the second folder
.venv\Scripts\python scripts\restore.py latest --only database,gallery       # only some parts
```

It checks the backup first, asks, stops GateSense and every camera (like stop.bat), moves the current files to
`data\backups\before-restore-<time>\` (nothing is deleted; the database's `-wal` and `-shm` files go there too, as
they must never stay next to a restored database), copies the backup in, checks the result and writes the restore to
the audit log. Then start GateSense with start.bat. If the backup is from before a GateSense update, its database is
upgraded at the first start as usual. Try a restore once on a spare PC before you need it.

After updating GateSense's code, restart it (`stop.bat`, then `start.bat`); start.bat rebuilds the web dashboard when it changed.

## Check the environment

```
.venv\Scripts\python scripts\check_env.py
```

On the first run this downloads the buffalo_s model (about 120 MB) to `%USERPROFILE%\.insightface\models`. It then opens the webcam, grabs one frame and prints how many faces it found and how long detection took, and whether the liveness models are in place.

To download and verify every model in one go (do it before a gate PC goes offline):

```
.venv\Scripts\python scripts\get_models.py           # download what's missing, check every SHA-256
.venv\Scripts\python scripts\get_models.py --check   # check only
```

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

Each face is tracked (ByteTrack); its best 5 frames are embedded, and when one person wins 3 of them an event is saved with a face snapshot in `data/snapshots/`. After an event, the same person in the same direction is ignored for the 5-minute cool-down, whichever camera of that direction sees them (two entrance cameras record one IN); the cool-down starts afresh at midnight. Add `--no-window` to log without video, or `--no-db` to tune without recording anything. Log lines look like:

```
13:28:34.814  track1    waqas        score=0.910 (waqas)          second=0.312 (ali)            ACCEPT
13:28:35.013  track2    unknown      score=0.402 (waqas)          second=0.298 (ali)            below threshold
13:28:35.140  track1    IN  waqas  EVENT saved  score=0.910  votes 3/3  snapshot=snapshots/2026-10-20/132835_waqas_entrance.jpg
13:31:02.517  track5    IN  waqas  ignored: cool-down (300s) since last IN
```

Use the scores to tune `MATCH_THRESHOLD` and `MATCH_MARGIN` in `app/config.py`.

## Liveness (anti-spoofing)

A phone screen or a printed photo held up to the camera is refused. Each face is checked by Silent-Face-Anti-Spoofing's two MiniFASNet models (2.7 and 4 times the face box, so the edge of a phone or a sheet of paper is in view) on the full-resolution frame, before it can vote. Their average "real" probability is the live score (0 to 1):

- below `LIVENESS_THRESHOLD` (0.8) the frame is skipped as a spoof: "skip: spoof (0.12)" in the Live decisions table (with the score in the Live column), a WARNING in the camera's log, and one review snapshot per track in `data/snapshots/<day>/spoof/` (deleted with the day's other snapshots after `SNAPSHOT_RETENTION_DAYS`);
- a person is recorded only when they win the vote and `LIVENESS_MIN_FRAMES` (3) of the track's frames passed the check;
- the enrollment wizard refuses a spoof too ("Use a real face, not a photo or a screen"), for the webcam and IP cameras. Photo files uploaded on the People page are not checked.

`LIVENESS_ENABLED`, `LIVENESS_THRESHOLD` and `LIVENESS_MIN_FRAMES` are in Settings > Recognition (restart running cameras to apply). With liveness on and its models missing (no internet for the first download), a camera refuses to start ("liveness model missing") rather than record without it.

**Models and licences.** The models download on first use (or with `scripts\get_models.py`) to `%USERPROFILE%\.gatesense\models` (`GATESENSE_MODEL_DIR`), and are used only if their SHA-256 matches `LIVENESS_MODELS` in `app/config.py`. They are [Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing) by MiniVision, **Apache-2.0**, in the ONNX export from [yakhyo/face-anti-spoofing](https://github.com/yakhyo/face-anti-spoofing) (also Apache-2.0). The exports were checked against MiniVision's official PyTorch weights: the same output to within 1e-6 on the same input. GateSense doesn't ship the files; if you redistribute them, include the Apache-2.0 licence and notices. (The buffalo_s face models remain non-commercial research use: see PLAN.md, Models.)

**Speed.** One check (both models, one face) takes about 8 to 17 ms on a 2-core i5-6300U. It runs only on faces of undecided tracks, so the worst case is a frame whose face is still being decided. Measured there: about 65-75 ms per frame without liveness and 79-93 ms with it (13-16 fps down to 11-13 fps at most; the worker processes 8). Measure your PC with a photo of one real face:

```
.venv\Scripts\python scriptsench_liveness.py photo.jpg --width 1920
```

**Testing it for real.** Start a camera, open Live cameras > full screen > Decisions (admin), then:

1. Walk past as yourself: decisions show "match ... live 1/3" up to "live 3/3" and an event, with Live scores near 1.
2. Hold up your enrollment photo on a phone, then a printout of it, at the distance you normally stand: each frame should show "skip ... spoof (0.xx)" in red, no event, a WARNING in the camera log and a snapshot in `data/snapshots/<today>/spoof/`. Try a laptop screen and a tablet too, at different angles and distances.
3. Watch the Live scores of real people at the real gate for a few days. If real people get refused (scores often below 0.8, e.g. in backlight or with small faces), lower `LIVENESS_THRESHOLD` a little; if a phone or photo ever gets through, raise it, or raise `LIVENESS_MIN_FRAMES`. Small faces (under about 100 px) give less reliable scores: get the camera closer.
4. In the enrollment wizard, hold up a phone with a photo: Capture stays disabled with "Use a real face, not a photo or a screen".

No software check stops every attack (a good mask, a high-resolution screen at the right distance). For stronger protection use an IR or depth camera.

## Today's attendance

```
.venv\Scripts\python scripts\today.py                    # today
.venv\Scripts\python scripts\today.py --date 2026-10-20
```

Prints name, first IN, last OUT, worked hours and the missing-exit and check flags for every enrolled person, with the same rule as the dashboard and the reports.

**One attendance per person per day:** the day is the first IN and the last OUT (local midnight to midnight); every other event is a *repeat* (kept, listed greyed with a "repeat" tag in the day's events, not used). Status:

| Status | When | Hours |
|---|---|---|
| `complete` | an IN, and the last OUT is after the first IN (an IN after the last OUT, e.g. IN 8, OUT 16, IN 17, is a repeat: 08:00-16:00, 8 h) | last OUT - first IN |
| `missing exit` | an IN but no OUT at all; for today this reads `inside` (screens and exports) | - |
| `missing entry` | OUTs but no IN | - |
| `check` | the last OUT is earlier than the first IN: someone should look | - |
| `absent` | no events | - |

"Present" means came in that day (complete, missing exit or check): the Overview's Present tile and the reports' Days present. A night shift (IN 23:50, OUT 00:10) is still two days; shift windows come later.

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
- **Reports**: the daily register (first IN, last OUT, hours worked, missing OUT, check) and the monthly summary per person
  (days present, total hours, missing exits, days to check) for any date range and department, exported as CSV or Excel.
- **Audit log**: every change with who, when and why; CSV export.
- **Settings**: cameras (add, edit, test the connection, reorder, delete, view and download the log), sites, users and
  roles, recognition thresholds, and system information with "Back up now".

To work on the web dashboard itself, run the app with **start-debug.bat** and, in a second window:

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
start-debug.bat                       (or: .venv\Scripts\python -m api.run)
```

It listens on `http://127.0.0.1:8000` (this PC only; change it with `GATESENSE_HOST` / `GATESENSE_PORT` in the environment or `.env`). `GET /api/health` returns the version, uptime and database status; interactive docs are at `/docs`. Press Ctrl+C in its window to stop it: camera workers it started are stopped first. A camera can only run in one process at a time: the API refuses a camera that the command line or the API already runs (or the webcam while the enrollment wizard holds it), and the worker itself locks the camera so two starts at the same moment can't both open it.

**Users and login.** The API has its own user accounts with four roles: **admin** (everything, including user accounts), **hr**, **viewer** and **guard** (the permissions of each are in `api/permissions.py`). On its first start the API creates the user `admin` with the `GATESENSE_ADMIN_PASSWORD`; that password must be changed at the first login. To try it, open `http://127.0.0.1:8000/docs`, run `POST /api/auth/login` with `{"username": "admin", "password": "..."}` (the page keeps the session cookie), then `POST /api/auth/password`, then the other endpoints. Admins manage accounts under `/api/users` (create with a temporary password, change role, deactivate, reset password; each change needs a reason). Sessions end after 8 hours without activity; 5 wrong passwords in a row lock an account for 15 minutes. Logins, failed logins, logouts and refused requests go to the audit log. On the first start after this update the database gets new tables (users, sessions, sites); a copy of the old database is saved in `data/backups/` first.

**Data endpoints** (used by the dashboard; all listed with their response types at `/docs`): `/api/overview` (the day at a glance: present, inside now, complete, missing exit, missing entry, check, absent, arrivals per hour, every camera's status), `/api/attendance` (daily rows, the same as the Overview), `/api/events` (raw IN/OUT events with score and snapshot; `/api/snapshots/{id}` shows a snapshot to logged-in users only), `/api/attendance/corrections` (HR and admin: add an IN/OUT, change a time, void or restore an event; the original time is kept and every correction needs a reason), `/api/people` (list, profile with photos and recent attendance, add, edit, deactivate, re-enroll), `/api/cameras` (list with status, add, edit, start, stop, and `/api/cameras/test` to open a camera once and see its resolution and fps), `/api/sites`, `/api/audit` and `/api/settings` (thresholds and the cool-down; admin only, each change audited, applied to cameras started afterwards). Lists take `page`, `page_size`, `sort` (e.g. `-date`), `q` (search) and, where it applies, `site_id`. Camera passwords never appear in a response: sources keep their `${VAR}` placeholders. A camera's region of interest can be saved, but the camera worker doesn't use it yet.

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
