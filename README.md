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

It prints, per person, how many photos were accepted and why any were rejected, then saves `data/gallery.npz`.

## Recognize

```
.venv\Scripts\python scripts\recognize.py                      # webcam from app/config.py
.venv\Scripts\python scripts\recognize.py --source 1           # another webcam
.venv\Scripts\python scripts\recognize.py --source clip.mp4    # recorded clip
```

Add `--no-window` to log without showing video. Each decision is logged like:

```
13:28:34.814  face0  waqas        score=0.910 (waqas)          second=0.312 (ali)            ACCEPT
13:28:35.013  face0  unknown      score=0.402 (waqas)          second=0.298 (ali)            below threshold
13:28:35.013  CONFIRMED waqas (3/5 frames)
```

Use these scores to tune `MATCH_THRESHOLD` and `MATCH_MARGIN` in `app/config.py`.

## Tests

```
.venv\Scripts\python -m pytest
```

## Configuration

Thresholds, frame rate, frame size and camera sources (webcam index or RTSP URL, plus IN/OUT direction) are in [app/config.py](app/config.py).

## Privacy

Face photos (`known/`) and embeddings (`data/`, `*.npz`) are biometric data. They are git-ignored and must never be committed. Enroll only people who have given written consent.
