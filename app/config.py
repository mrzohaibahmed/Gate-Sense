"""Central configuration.

All thresholds, frame-rate limits and camera settings live here so they can be
tuned in one place. Other modules import from this file instead of hard-coding
values.
"""

import os
from pathlib import Path

# --- Paths ---------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
KNOWN_DIR = ROOT_DIR / "known"  # known/<employee code>/*.jpg enrollment photos
DATA_DIR = Path(os.environ.get("GATESENSE_DATA_DIR") or ROOT_DIR / "data")  # override for tests or another disk
GALLERY_PATH = DATA_DIR / "gallery.npz"
DB_PATH = DATA_DIR / "attendance.db"
SNAPSHOT_DIR = DATA_DIR / "snapshots"  # one small face crop per attendance event
RUN_DIR = DATA_DIR / "run"  # worker status (<camera>.json) and stop (<camera>.stop) files
LOG_DIR = DATA_DIR / "logs"  # worker logs (<camera>.log)
LIVE_DIR = DATA_DIR / "live"  # latest annotated frame (<camera>.jpg) and decisions (<camera>.json)

# --- Models ----------------------------------------------------------------
MODEL_PACK = "buffalo_s"  # InsightFace pack: SCRFD-500M detector + MobileFaceNet ArcFace
PROVIDERS = ["CPUExecutionProvider"]  # explicit, so the Azure provider is never used
CTX_ID = -1  # -1 = CPU, 0 = first GPU
MODEL_MODULES = ["detection", "recognition"]  # skip the pack's gender/age and landmark models

# --- Detection -------------------------------------------------------------
DET_SIZE = (640, 640)  # detector input size
FRAME_WIDTH = 640  # frames are resized to this width for detection only (quality, embedding: full frame)
DET_THRESHOLD = 0.5  # minimum detector confidence

# --- Quality filter (applied before embedding) ----------------------------
MIN_FACE_PX = 80  # minimum face box width in camera pixels (full-resolution frame, not the FRAME_WIDTH copy)
MIN_BLUR_VAR = 60.0  # minimum variance of Laplacian on the face resized to 112x112 (lower = blurrier)
MAX_YAW_RATIO = 0.30  # nose offset from the eye midpoint / eye distance (0 = facing straight on)
PITCH_RANGE = (0.25, 0.75)  # (nose - eyes) / (mouth - eyes) vertically; about 0.5 when level

# --- Recognition -----------------------------------------------------------
EMBEDDING_DIM = 512
MATCH_THRESHOLD = 0.45  # minimum cosine similarity to accept a match
MATCH_MARGIN = 0.05  # top score must beat the second-best person by this much

# --- Tracking (ByteTrack via supervision) and voting --------------------------
BEST_FRAMES_PER_TRACK = 5  # embed and keep only the best N frames of each tracked face
VOTE_MIN_AGREE = 3  # a person must win this many of a track's kept frames to be recorded
TRACK_ACTIVATION_THRESHOLD = 0.5  # detector score needed to start a new track
TRACK_MATCH_THRESHOLD = 0.8  # ByteTrack matching threshold (higher = more lenient box matching)
TRACK_LOST_SECONDS = 1.5  # a track not seen for this long is closed

# --- Liveness (anti-spoofing: a phone screen or a printed photo held up to the camera) ------------------
# Silent-Face-Anti-Spoofing (MiniVision, Apache-2.0): two MiniFASNet models on 80x80 crops of the full-resolution
# frame, 2.7 and 4 times the face box (the background around the face is what gives a screen or a print away).
# The live score is their average "real" probability (0-1). A face below LIVENESS_THRESHOLD is a spoof: it never
# votes, and a track is recorded only after LIVENESS_MIN_FRAMES of its frames passed. The enrollment wizard
# refuses a spoof too (photo files uploaded on the People page are not checked).
LIVENESS_ENABLED = True
LIVENESS_THRESHOLD = 0.7  # minimum live score (0.8 refused too many real people at the gate)
LIVENESS_MIN_FRAMES = 3  # frames of a track that must pass before it can be recorded
SPOOF_SNAPSHOT_SIZE = 320  # longest side of the review snapshot of a spoof (data/snapshots/<day>/spoof/)
LIVENESS_THREADS = 1  # onnxruntime threads per liveness model (tiny models: more threads only slow the others down)
# The ONNX exports of the official weights (yakhyo/face-anti-spoofing, Apache-2.0), verified by SHA-256 after
# download (scripts/get_models.py, or automatically on first use). file: (url, sha256, crop scale).
LIVENESS_MODEL_DIR = Path(os.environ.get("GATESENSE_MODEL_DIR") or Path.home() / ".gatesense" / "models")
LIVENESS_MODELS = {
    "MiniFASNetV2.onnx": (
        "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx",
        "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907", 2.7),
    "MiniFASNetV1SE.onnx": (
        "https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV1SE.onnx",
        "ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676", 4.0),
}

# --- Enrollment ----------------------------------------------------------------
ENROLL_MIN_PHOTOS = 5  # warn when a person has fewer accepted photos
# Enrollment photo checks (app/enroll_quality.py). Head pose comes from InsightFace's 3D-landmark model.
# These apply to enrollment only; the live cameras keep MAX_YAW_RATIO / PITCH_RANGE above.
ENROLL_MAX_YAW = 35.0  # degrees turned left/right; beyond this a photo is refused
ENROLL_MAX_PITCH = 25.0  # degrees tilted up/down
ENROLL_DET_THRESHOLD = 0.4  # face detector confidence at enrollment (live cameras use DET_THRESHOLD)
ENROLL_MIN_BRIGHTNESS = 40  # mean brightness of the face (0-255); darker = "Too dark"

# Guided enrollment (Enrollment page: live camera guide, or simple capture). Yaw ranges are from the
# PERSON's point of view: positive = turned to their own left, negative = to their own right. Steps with
# "required": False are optional extras (e.g. add {"key": "up", ..., "pitch": (5, 20), "required": False}).
ENROLL_STEPS = [
    {"key": "front", "title": "Front", "instruction": "Look straight at the camera.", "arrow": "⬆️",
     "yaw": (-10, 10), "pitch": (-20, 20), "required": True},
    {"key": "left", "title": "Slightly left", "instruction": "Turn your head slightly to your left.", "arrow": "⬅️",
     "yaw": (15, 35), "pitch": (-20, 20), "required": True},
    {"key": "right", "title": "Slightly right", "instruction": "Turn your head slightly to your right.", "arrow": "➡️",
     "yaw": (-35, -15), "pitch": (-20, 20), "required": True},
]
ENROLL_GUIDE_HEIGHT = 0.62  # guide oval height as a fraction of the frame height (centred)
ENROLL_GUIDE_ASPECT = 0.78  # guide oval width / height: face-shaped whatever the camera's aspect ratio
ENROLL_FACE_MAX_FRACTION = 0.95  # a face wider than this share of the guide box = "Move back a little"
ENROLL_SELF_MATCH_MIN = 0.45  # the captured photos must match each other at least this well (cosine)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
IMPORT_MAX_ROWS = 5000  # people per file in People > Import (app/people_import.py)
ENROLL_MAX_PHOTO_PX = 1600  # dashboard uploads are shrunk to this longest side before saving

# --- Camera workers (started from the dashboard) ----------------------------
WORKER_STATUS_SECONDS = 2.0  # how often a worker writes data/run/<camera>.json
WORKER_STALE_SECONDS = 20.0  # status older than this = "not responding"
WORKER_STOP_TIMEOUT = 10.0  # seconds to wait for a clean stop before killing
WORKER_LOG_MAX_BYTES = 10_000_000  # data/logs/<camera>.log is rotated at this size, also while the worker runs
WORKER_LOG_BACKUPS = 5  # rotated logs kept: <camera>.log.1 (newest) ... .log.5; older ones are deleted
# Per-frame lines (a face skipped for quality or as a spoof) only with this on (or recognize.py --debug); otherwise
# they are counted in the once-a-minute "stats:" line. GATESENSE_WORKER_DEBUG=1 turns it on for every worker.
WORKER_DEBUG_LOG = os.environ.get("GATESENSE_WORKER_DEBUG") == "1"
WORKER_LOG_LINES = 50  # log lines shown per camera
GALLERY_RELOAD_SECONDS = 5.0  # workers reload gallery.npz this often if it changed
GALLERY_RETRY_SECONDS = 5.0  # a gallery.npz that can't be read at a worker's start is tried again this often
# Cameras with "enabled": the app starts them when it starts and restarts any that stops (api/supervisor.py),
# waiting SUPERVISOR_BACKOFF seconds between attempts (the last value repeats). A worker that ran for
# SUPERVISOR_STABLE_SECONDS counts as healthy again: its next restart waits the first delay.
SUPERVISOR_CHECK_SECONDS = 2.0
SUPERVISOR_BACKOFF = (5, 10, 20, 40, 80, 160, 300)
SUPERVISOR_STABLE_SECONDS = 300
EVENT_QUEUE_MAX = 1000  # events a worker keeps in memory while the database can't be written (retried)
CAMERA_WARN_SECONDS = 5.0  # warn when a live camera sends no frames, or only black ones, for this long
BLACK_FRAME_LEVEL = 3.0  # mean brightness (0-255) below this counts as a black frame

# --- RTSP IP cameras (e.g. Hikvision: rtsp://user:${GATESENSE_CAM_PASS}@ip:554/Streaming/Channels/101) ---
# FFmpeg options for every rtsp:// source: TCP transport (no lost UDP packets) and a 5 s socket
# timeout in microseconds. "stimeout" is the same timeout for FFmpeg builds older than 5.0; newer
# builds (OpenCV 5.0 ships FFmpeg 7.1) ignore it. An OPENCV_FFMPEG_CAPTURE_OPTIONS environment
# variable that is already set wins over this.
RTSP_FFMPEG_OPTIONS = "rtsp_transport;tcp|timeout;5000000|stimeout;5000000"
RTSP_OPEN_TIMEOUT_MS = 5000  # CAP_PROP_OPEN_TIMEOUT_MSEC
RTSP_READ_TIMEOUT_MS = 5000  # CAP_PROP_READ_TIMEOUT_MSEC
RTSP_STALE_SECONDS = 5.0  # no new frame for this long = disconnected, reconnect
RTSP_CONNECT_GRACE_SECONDS = 12.0  # time allowed for open + first frame before giving up on an attempt
RTSP_RECONNECT_BACKOFF = (1, 2, 4, 8, 16, 30)  # seconds between attempts; stays at the last value
# Webcams (app.camera.LiveCapture): reopened after WEBCAM_READ_FAILURES failed reads in a row, or a failed open,
# waiting these seconds between attempts (resets after frames arrive again). The worker never gives up.
WEBCAM_READ_FAILURES = 50
WEBCAM_RECONNECT_BACKOFF = (1, 2, 4, 8, 15, 30)

# --- Live view ------------------------------------------------------------------
LIVE_FRAME_SECONDS = 0.5  # workers write their latest annotated frame at most this often
LIVE_JPEG_QUALITY = 75
LIVE_DECISIONS = 20  # recent decisions kept per camera for the live view
LIVE_STALE_SECONDS = 5.0  # an older frame shows "camera not sending frames"
# Live video through the API (GET /api/cameras/{id}/stream). While someone watches, the API keeps
# data/live/<camera>.watch fresh and the worker writes its annotated frame up to STREAM_FPS times a
# second, drawn up to STREAM_MAX_WIDTH wide from the full camera frame; otherwise it stays at
# LIVE_FRAME_SECONDS and the detection size. The frame is encoded once and shared by every viewer.
STREAM_FPS = 8
STREAM_MAX_WIDTH = 960
STREAM_WATCH_SECONDS = 3.0  # a .watch file older than this = nobody is watching
WS_POLL_SECONDS = 1.0  # /api/ws: new events and corrections are looked for this often (while clients are connected)
WS_CAMERA_SECONDS = 2.0  # /api/ws: camera status messages (and session checks) this often
WS_QUEUE = 200  # messages waiting for one WebSocket client; a client this far behind is disconnected
ENROLL_CAMERA_CLAIM_SECONDS = 90  # the enrollment wizard's webcam claim lapses if not renewed within this time
ENROLL_CAMERA_RENEW_SECONDS = 20  # the wizard renews its claim this often while it has the webcam open
# Enrollment from an IP camera (the wizard's "Which camera?"): the server reads the camera and checks the
# centred enrollment window, the same guide box and steps as the webcam, at the camera's full resolution.
ENROLL_IP_TYPES = ("hikvision", "rtsp")  # camera types offered in the wizard
ENROLL_IP_WINDOW = 0.5  # the enrollment window: this share of the frame's width and height, centred
ENROLL_FEED_FPS = 5  # a running worker shares this many full-resolution frames a second with the wizard
ENROLL_FEED_WATCH_SECONDS = 3  # ... while the API keeps data/live/<camera>.enroll this fresh
ENROLL_PREVIEW_FPS = 8  # the wizard's IP camera preview (MJPEG): frames a second
ENROLL_PREVIEW_WIDTH = 960  # ... and its largest width (the checks and the photo use the full resolution)
ENROLL_CAPTURE_QUALITY = 95  # JPEG quality of the frames a worker shares and of a captured photo

# --- Processing rate -------------------------------------------------------
PROCESS_FPS = 8  # frames per second actually processed (5 to 10 on CPU)
# onnxruntime threads for face detection and recognition in each camera worker (and the API's enrollment models).
# 0 = automatic: the CPU's cores shared out between the cameras in the list (at least 1 each), so two workers don't
# both try to use every core. A worker reads it when it starts.
ONNX_THREADS = 0
SNAPSHOT_RETENTION_DAYS = 30  # the nightly job deletes event snapshots older than this (the events stay)

# --- Attendance --------------------------------------------------------------
# Ignore the same person in the same direction (any camera of it) for this long after a recorded event. There is no
# reset at midnight, so a night shift's passes either side of it are one cool-down; that is also why the editable
# maximum is an hour (a longer one could swallow the next shift's pass).
COOLDOWN_SECONDS = 5 * 60
SNAPSHOT_SIZE = 160  # longest side of saved face snapshots, in pixels

# --- Shifts and roster (app/shifts.py) ------------------------------------------------
# Each shift in the shifts table has its own grace and windows; these are the defaults for a new one. The IN window
# runs from in_window_before_min before the start to the shift's midpoint, the OUT window from the midpoint to
# out_window_after_min after the end. A night shift belongs to the date it starts.
SHIFT_GRACE_MIN = 30  # IN up to start + grace is on time; OUT before end - grace is an early leave
SHIFT_IN_WINDOW_BEFORE_MIN = 120
SHIFT_OUT_WINDOW_AFTER_MIN = 240
SHIFT_MINUTES_MAX = 720  # the most a grace or a window can be (12 h)
DEFAULT_SHIFTS = [  # put into the shifts table when it is created (schema step 5); edited in Settings > Shifts
    {"name": "Morning", "start": "06:30", "end": "14:30", "crosses_midnight": False},
    {"name": "General", "start": "08:00", "end": "16:30", "crosses_midnight": False},
    {"name": "Night", "start": "22:00", "end": "06:00", "crosses_midnight": True},
]
ROSTER_MAX_DAYS = 62  # dates per roster request or roster file (the Roster screen shows a week)
ROSTER_MAX_ENTRIES = 10000  # cells per roster save

# --- Admin password and .env ---------------------------------------------------
DASHBOARD_PASSWORD_ENV = "GATESENSE_ADMIN_PASSWORD"  # admin password: environment variable, or .env
# Optional .env (admin password, camera passwords); real environment variables take priority (see .env.example).
ENV_PATH = Path(os.environ.get("GATESENSE_ENV_FILE") or ROOT_DIR / ".env")

# --- API (FastAPI, api/) --------------------------------------------------------
# GateSense runs as one app: the API also serves the built web dashboard (web/dist) at /.


def _server_setting(name: str) -> str | None:
    """A server setting from the environment, else from .env (read here only; .env is never loaded wholesale)."""
    if os.environ.get(name):
        return os.environ[name]
    path = Path(os.environ.get("GATESENSE_ENV_FILE") or ROOT_DIR / ".env")
    if path.is_file():
        from dotenv import dotenv_values

        return dotenv_values(path).get(name) or None
    return None


API_HOST = _server_setting("GATESENSE_HOST") or "127.0.0.1"  # bind address: 127.0.0.1 = this PC only
API_PORT = int(_server_setting("GATESENSE_PORT") or 8000)
# HTTPS: both files (PEM) to serve over https. Required when API_HOST isn't this PC, unless GATESENSE_ALLOW_HTTP=1
# (then a warning: passwords and the session cookie would cross the network unencrypted).
SSL_CERTFILE = _server_setting("GATESENSE_SSL_CERTFILE")
SSL_KEYFILE = _server_setting("GATESENSE_SSL_KEYFILE")
ALLOW_HTTP = _server_setting("GATESENSE_ALLOW_HTTP") == "1"
HTTPS = bool(SSL_CERTFILE and SSL_KEYFILE)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
API_CORS_ORIGINS = ["http://localhost:5173"]  # browsers may call the API only from the Vite dev server
WEB_DIST = Path(os.environ.get("GATESENSE_WEB_DIST") or ROOT_DIR / "web" / "dist")  # the built React app
MAX_REQUEST_BYTES = 10_000_000  # larger request bodies are refused (413): enrollment photos are the biggest
LOGIN_RATE_LIMIT = 10  # login attempts per IP address per LOGIN_RATE_WINDOW_SECONDS (on top of the account lockout)
LOGIN_RATE_WINDOW_SECONDS = 60
# Database URL for the API's data-access module (api/db.py). None = SQLite at DB_PATH. sqlite:/// URLs work
# today; a postgresql:// URL is recognised and reported as not supported until that backend is written.
DATABASE_URL = os.environ.get("GATESENSE_DATABASE_URL") or None

# --- API users and login (api/auth.py; roles and permissions: api/permissions.py) ----------------
API_FIRST_ADMIN = "admin"  # created on the API's first start (no users yet) with the GATESENSE_ADMIN_PASSWORD
SESSION_COOKIE = "gatesense_session"
SESSION_HOURS = 8  # a session ends after this long without activity; each request extends it
SESSION_REFRESH_SECONDS = 60  # extend the session (database write + new cookie) at most this often
SESSION_COOKIE_SECURE = HTTPS  # over HTTPS the cookie is marked Secure (never sent over plain HTTP)
LOGIN_MAX_FAILURES = 5  # wrong passwords in a row before the account is locked
LOGIN_LOCK_MINUTES = 15
PASSWORD_MIN_LENGTH = 10  # for passwords set through the API (the first admin password is changed at first login)
DEFAULT_SITE_NAME = "Main site"  # every camera belongs to a site; existing ones go here

# --- Nightly maintenance (api/maintenance.py, while the app runs) -----------------------------------
BACKUP_HOUR = 2  # the nightly backup runs once a day at or after this hour (local time)
# A backup set: the database (checked with PRAGMA integrity_check), the face gallery, the enrollment photos
# (known/, as a zip), cameras.json and settings.json. Never .env (passwords): keep a copy of it somewhere safe.
BACKUP_KEEP = 14  # nightly sets kept in each backup folder (manual and upgrade copies are never deleted)
# A second folder for a copy of every nightly and manual set: another disk or a USB drive, so one failed disk
# doesn't take the backups with it. GATESENSE_BACKUP_COPY_DIR in the environment or .env; None = no copy.
BACKUP_COPY_DIR = Path(_server_setting("GATESENSE_BACKUP_COPY_DIR")) if _server_setting(
    "GATESENSE_BACKUP_COPY_DIR") else None
MAINTENANCE_CHECK_SECONDS = 600  # how often the app checks whether the nightly job is due

# --- Health (GET /api/health, the Overview's warning banner) ------------------------------------------
HEALTH_FRAME_STALE_SECONDS = 30  # a running camera without a new frame for this long is a problem
HEALTH_MIN_FREE_BYTES = 2_000_000_000  # less free space than this on the data disk is a problem

# --- Cameras ---------------------------------------------------------------
# Cameras are normally managed in the dashboard's Settings > Cameras, which saves them to
# CAMERAS_FILE (cameras.json in the project folder; no passwords in it). When that file exists it
# replaces DEFAULT_CAMERAS below; delete it to go back to these defaults.
# Run one recognize.py per camera: --camera <name>. The first entry is the default.
# source: an int for a local webcam index, a video file path, or an RTSP URL.
#   Never put a camera password here: use a ${VAR} placeholder and set the variable in .env
#   (or the environment), e.g. "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101".
#   The password is URL-encoded when the stream is opened, so @ # / : in it are fine.
# direction: "IN" for entrance, "OUT" for exit.
# mirror (optional): show the live view flipped left-right, like a mirror. Display only; recognition
#   uses the original frame. Handy for a webcam facing you; usually off for gate cameras.
# enabled (optional): the app starts this camera when it starts and restarts it whenever it stops
#   (Settings > Cameras "Auto"). A Stop on the Live cameras page pauses that until the camera is started again.
DEFAULT_CAMERAS = [
    {"name": "entrance", "source": 0, "direction": "IN", "mirror": True, "enabled": False},
    # Testing with one webcam: entrance and exit share webcam 0, so run one at a time (two at once is refused).
    # With a second USB webcam, set this to 1 to run both together.
    {"name": "exit", "source": 0, "direction": "OUT", "mirror": True, "enabled": False},
    # {"name": "gate-entrance", "direction": "IN",
    #  "source": "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101"},
    # {"name": "gate-exit", "direction": "OUT",
    #  "source": "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.102:554/Streaming/Channels/101"},
]
CAMERAS_FILE = Path(os.environ.get("GATESENSE_CAMERAS_FILE") or ROOT_DIR / "cameras.json")

# --- Settings changed through the API (PATCH /api/settings) ------------------------------------------
# Only these can be changed there. The changes are saved to SETTINGS_FILE (settings.json, git-ignored)
# and applied over the values above when this module is imported, so app/config.py stays the one place
# the code reads them from. A camera worker reads them when it starts: restart running cameras to apply.
# name: (type, minimum, maximum, meaning)
EDITABLE_SETTINGS = {
    "MATCH_THRESHOLD": (float, 0.2, 0.9, "minimum cosine similarity to accept a match"),
    "MATCH_MARGIN": (float, 0.0, 0.5, "the best person must beat the second-best by this much"),
    "DET_THRESHOLD": (float, 0.1, 0.95, "minimum face detector confidence"),
    "MIN_FACE_PX": (int, 20, 1000, "minimum face width in camera pixels"),
    "MIN_BLUR_VAR": (float, 0.0, 1000.0, "minimum sharpness (variance of Laplacian)"),
    "BEST_FRAMES_PER_TRACK": (int, 1, 20, "frames embedded and kept per tracked face"),
    "VOTE_MIN_AGREE": (int, 1, 20, "kept frames one person must win to be recorded (at most BEST_FRAMES_PER_TRACK)"),
    "TRACK_LOST_SECONDS": (float, 0.2, 30.0, "a track not seen for this long is closed"),
    "LIVENESS_ENABLED": (bool, 0, 1, "refuse phone screens and printed photos (anti-spoofing)"),
    "LIVENESS_THRESHOLD": (float, 0.1, 0.99, "minimum live score (0-1); a face below it is treated as a spoof"),
    "LIVENESS_MIN_FRAMES": (int, 1, 20, "frames of a tracked face that must pass the liveness check to be recorded"),
    "COOLDOWN_SECONDS": (int, 0, 3600, "debounce: the same person in the same direction is ignored for this long "
                                       "(no reset at midnight)"),
    "PROCESS_FPS": (int, 1, 30, "frames processed per second"),
    "ONNX_THREADS": (int, 0, 64, "threads per camera worker for face detection and recognition; 0 = automatic "
                                 "(CPU cores / cameras)"),
    "SNAPSHOT_RETENTION_DAYS": (int, 1, 3650, "face snapshots of events are deleted after this many days"),
}
SETTINGS_FILE = Path(os.environ.get("GATESENSE_SETTINGS_FILE") or ROOT_DIR / "settings.json")
DEFAULT_SETTINGS = {name: globals()[name] for name in EDITABLE_SETTINGS}  # the values written above
_settings_signature = None


def check_setting(name: str, value):
    """The value converted to the setting's type; ValueError if it isn't editable or is out of range."""
    if name not in EDITABLE_SETTINGS:
        raise ValueError(f"{name} can't be changed here")
    kind, low, high, _ = EDITABLE_SETTINGS[name]
    if kind is bool:  # on/off: true/false, or 1/0
        if not isinstance(value, (bool, int, float)) or value not in (0, 1):
            raise ValueError(f"{name} must be on or off (true/false)")
        return bool(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind is int and value != int(value)):
        raise ValueError(f"{name} must be {'a whole number' if kind is int else 'a number'}")
    value = kind(value)
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def load_settings(path: Path | None = None) -> dict:
    """Apply SETTINGS_FILE over the defaults; returns the overrides applied. A bad file or value is skipped."""
    import json

    global _settings_signature
    path = path or SETTINGS_FILE
    _settings_signature = _signature(path)
    values = dict(DEFAULT_SETTINGS)
    overrides = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))["settings"]
            for name, value in stored.items():
                try:
                    overrides[name] = values[name] = check_setting(name, value)
                except ValueError as e:
                    print(f"WARNING: {path.name}: {e}; using the default")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            print(f"WARNING: could not read {path} ({e}); using the settings in app/config.py")
            overrides = {}
            values = dict(DEFAULT_SETTINGS)
    if values["VOTE_MIN_AGREE"] > values["BEST_FRAMES_PER_TRACK"]:
        print("WARNING: VOTE_MIN_AGREE is more than BEST_FRAMES_PER_TRACK; using the defaults for both")
        for name in ("VOTE_MIN_AGREE", "BEST_FRAMES_PER_TRACK"):
            values[name] = DEFAULT_SETTINGS[name]
            overrides.pop(name, None)
    globals().update(values)
    return overrides


def refresh_settings() -> bool:
    """Reload the settings if SETTINGS_FILE changed since the last load (like refresh_cameras). True if reloaded."""
    if _signature(SETTINGS_FILE) == _settings_signature:
        return False
    load_settings()
    return True


_cameras_signature = None  # (mtime, size) of the cameras file when CAMERAS was last loaded; None = no file
CAMERAS_ERROR: str | None = None  # why CAMERAS_FILE can't be read; set = GateSense and the workers refuse to start


class CamerasFileError(ValueError):
    """CAMERAS_FILE exists but can't be read. Never replaced by the defaults: they would start the wrong cameras
    (webcam 0 instead of the gate's IP cameras), and Settings > Cameras would then save them over the file."""


def _signature(path: Path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def refresh_cameras() -> bool:
    """Reload CAMERAS if CAMERAS_FILE was changed, created or deleted since the last load (the dashboard
    calls this on every page run, so hand edits apply without a restart). True if reloaded.

    A file that became unreadable while running keeps the cameras loaded before, and sets CAMERAS_ERROR."""
    global CAMERAS, CAMERAS_ERROR
    if _signature(CAMERAS_FILE) == _cameras_signature:
        return False
    try:
        CAMERAS = load_cameras()
    except CamerasFileError as e:
        if CAMERAS_ERROR != str(e):
            print(f"WARNING: {e} Keeping the cameras loaded before.")
        CAMERAS_ERROR = str(e)
        return False
    CAMERAS_ERROR = None
    return True


def check_cameras() -> None:
    """Raise CamerasFileError if CAMERAS_FILE can't be read (the app's start and each worker's start call this)."""
    refresh_cameras()
    if CAMERAS_ERROR:
        raise CamerasFileError(CAMERAS_ERROR)


def load_cameras(path: Path | None = None) -> list[dict]:
    """Cameras from CAMERAS_FILE if it exists (written by Settings > Cameras), else DEFAULT_CAMERAS.

    A file that exists but can't be read raises CamerasFileError; it is never replaced by the defaults.
    `enabled`: the app starts the camera by itself and restarts it if it stops (api/supervisor.py).
    """
    global _cameras_signature
    path = path or CAMERAS_FILE
    _cameras_signature = _signature(path)
    if not path.is_file():
        return [dict(c) for c in DEFAULT_CAMERAS]
    return parse_cameras(path)


def parse_cameras(path: Path) -> list[dict]:
    """The cameras in a cameras.json file; CamerasFileError if it can't be read or lists none."""
    import json

    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["cameras"]
        cameras = [{"name": e["name"], "source": e["source"], "direction": e["direction"],
                    "mirror": bool(e.get("mirror")), "enabled": bool(e.get("enabled"))} for e in entries]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CamerasFileError(
            f"{path} can't be read ({type(e).__name__}: {e}). Fix the file, or restore it from a backup "
            "(scripts\\restore.py); GateSense won't start cameras it isn't sure about.") from None
    if not cameras:
        raise CamerasFileError(f"{path} has no cameras. Add one, or delete the file to use the defaults.")
    return cameras


def _first_cameras() -> list[dict]:
    global CAMERAS_ERROR
    try:
        return load_cameras()
    except CamerasFileError as e:
        CAMERAS_ERROR = str(e)
        print(f"ERROR: {e}")
        return []


CAMERAS = _first_cameras()
load_settings()
