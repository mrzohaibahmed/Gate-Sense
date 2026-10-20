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

## Check the environment

```
.venv\Scripts\python scripts\check_env.py
```

On the first run this downloads the buffalo_s model (about 120 MB) to `%USERPROFILE%\.insightface\models`. It then opens the webcam, grabs one frame and prints how many faces it found and how long detection took.

## Enroll people

Put 5 to 10 photos per person in `known/<name>/` (see [known/README.md](known/README.md)), then:

```
.venv\Scripts\python scripts\enroll.py
```

Or capture them from the webcam (SPACE saves when the face passes the quality check, Q quits):

```
.venv\Scripts\python scripts\enroll.py --capture waqas
```

It prints, per person, how many photos were accepted and why any were rejected, then saves `data/gallery.npz` and syncs the `persons` table in `data/attendance.db`.

## Record attendance

Run one worker per camera. Cameras (name, source, IN/OUT direction) are listed in `CAMERAS` in [app/config.py](app/config.py); `--source` overrides a camera's source. To test with one webcam, run two terminals:

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

Set the admin password in an environment variable (it is never stored in the code), then start the dashboard:

```
$env:GATESENSE_ADMIN_PASSWORD = "choose-a-strong-password"     # PowerShell
set GATESENSE_ADMIN_PASSWORD=choose-a-strong-password          # cmd
.venv\Scripts\python scripts\dashboard.py
```

Open http://localhost:8501. It listens on localhost only (`DASHBOARD_HOST` in `app/config.py`) and Streamlit's usage statistics are turned off. Pages:

- **Live events**: the latest events with snapshot thumbnails, refreshing every 5 seconds.
- **Today**: counts and a table of who is present, missing an exit, missing an entry or absent.
- **Reports**: daily or monthly reports, downloadable as Excel or CSV.
- **People**: list everyone, edit name, department and shift, activate or deactivate. Inactive people are not recorded by the cameras.
- **Corrections**: add a manual event (for example a missing OUT), change an event's time, or void and restore an event. A reason is required and every change goes to the audit log, which can be viewed and exported. Nothing is deleted.

## Tests

```
.venv\Scripts\python -m pytest
```

## Configuration

Thresholds, frame rate, frame size, tracking, vote and cool-down settings, and the camera list (name, source, IN/OUT direction) are in [app/config.py](app/config.py).

## Privacy

Face photos (`known/`), embeddings (`data/`, `*.npz`) and event snapshots (`data/snapshots/`) are biometric data. They are git-ignored and must never be committed. Enroll only people who have given written consent.
