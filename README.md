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

Then set the dashboard's admin password: copy `.env.example` to `.env` and fill in `GATESENSE_ADMIN_PASSWORD=`. `.env` is git-ignored. A real `GATESENSE_ADMIN_PASSWORD` environment variable, if set, takes priority over `.env`.

```
copy .env.example .env
```

## Start and stop (Windows)

Double-click **`start.bat`** in the project folder. It checks the setup and the admin password, starts the dashboard in a minimized "GateSense dashboard" window and opens http://localhost:8501 in your browser. If GateSense is already running it just opens the browser. Start the cameras from the dashboard's **Cameras** page.

Double-click **`stop.bat`** to stop everything: every camera worker is stopped cleanly first (also ones started from the command line), then the dashboard. Closing the "GateSense dashboard" window also stops it; camera workers notice and exit on their own.

If something goes wrong, the window stays open with the reason (for example a missing password or port 8501 in use).

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

Set a camera's `"source"` in `CAMERAS` in [app/config.py](app/config.py) to its RTSP URL, with a `${VAR}` placeholder instead of the password (never put the password in config.py). For Hikvision usually:

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

For 20 seconds (or `--seconds`) it prints the codec, resolution, input fps, time per frame, and the width and score of every face it detects. Walk past the camera while it runs. The summary gives the median face width in the frame the worker processes (resized to 640 px) and warns "faces too small" if it is under 80 px. One snapshot with the face boxes is saved to `data/probe/<camera>_<time>.jpg` (git-ignored). Passwords are masked.

## Record attendance

Run one worker per camera, either with the Start buttons on the dashboard's **Cameras** page or from the command line. Cameras (name, source, IN/OUT direction) are listed in `CAMERAS` in [app/config.py](app/config.py); `--source` overrides a camera's source. To test with one webcam, run two terminals:

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

The admin password comes from `GATESENSE_ADMIN_PASSWORD` in `.env` (see Setup) or from the environment, which wins if both are set. It is never stored in the code or printed; the launcher only says where it was found. Without a password the dashboard refuses to start.

```
start.bat                                          # or, in this window:
.venv\Scripts\python scripts\dashboard.py
```

Open http://localhost:8501. Stop with `stop.bat` (or Ctrl+C in the dashboard window). It listens on localhost only (`DASHBOARD_HOST` in `app/config.py`) and Streamlit's usage statistics are turned off. Pages:

- **Live events**: the latest recorded attendance events with snapshot thumbnails, refreshing every 5 seconds. Only events show here: a face appears once its vote is won, and not again during the 5-minute cool-down.
- **Live view**: each running camera's latest processed frame (face boxes, track ID, name or "unknown", score, skip reason), refreshing every second, with fps and frame age, plus its last 20 decisions: skips, unknowns (below threshold, low margin), matches with the vote count, events and cool-downs. A frame older than 5 seconds shows "camera not sending frames". The same view is at the top of the Cameras page.
- **Today**: counts and a table of who is present, missing an exit, missing an entry or absent.
- **Reports**: daily or monthly reports, downloadable as Excel or CSV.
- **People**: list everyone, edit name, department and shift, activate or deactivate. Inactive people are not recorded by the cameras.
- **Enrollment**: add a person (employee code, name, department, shift), then add photos by uploading files or taking them with the camera (one per click). Each photo is checked with the same quality filter as `enroll.py` and shown as accepted or rejected with the reason; accepted photos go to `known/<code>/` and the person's embeddings are rebuilt. Delete single photos, or **Delete face data** (photos and embeddings; attendance history is kept; needs a reason and is audited). The camera is off until you switch on **Use the camera**; switch it off when done, because a webcam camera worker can't start (or gets only black frames) while the browser holds the webcam. The camera is refused while a camera worker is using the webcam.
- **Cameras**: Start and Stop the `recognize.py` worker for each camera, with status (running/stopped, since when, fps) and the last 50 log lines (`data/logs/<camera>.log`). Two workers on the same webcam are refused, and so is a webcam worker while the Enrollment camera is switched on. If the camera sends no frames, or only black ones (usually another app holding the webcam), the worker shows a warning here and in its log. Workers started here stop when the dashboard stops; workers already running (for example started from the command line) are detected and can be stopped. Running workers pick up enrollment changes within a few seconds.
- **Settings**: read-only view of the thresholds, vote and cool-down settings and camera sources (RTSP passwords hidden).
- **Corrections**: add a manual event (for example a missing OUT), change an event's time, or void and restore an event. A reason is required and every change goes to the audit log, which can be viewed and exported. Nothing is deleted.

## Tests

```
.venv\Scripts\python -m pytest
```

## Configuration

Thresholds, frame rate, frame size, tracking, vote and cool-down settings, and the camera list (name, source, IN/OUT direction) are in [app/config.py](app/config.py).

## Privacy

Face photos (`known/`), embeddings (`data/`, `*.npz`) and event snapshots (`data/snapshots/`) are biometric data. They are git-ignored and must never be committed. Enroll only people who have given written consent.
