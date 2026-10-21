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
FRAME_WIDTH = 640  # frames are resized to this width before detection
DET_THRESHOLD = 0.5  # minimum detector confidence

# --- Quality filter (applied before embedding) ----------------------------
MIN_FACE_PX = 80  # minimum face box width in pixels
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
ENROLL_STABLE_SECONDS = 0.7  # the guide must stay green this long before Capture is enabled
ENROLL_AUTO_CAPTURE = False  # capture by itself after ENROLL_AUTO_CAPTURE_SECONDS of stable green
ENROLL_AUTO_CAPTURE_SECONDS = 1.0
ENROLL_ANALYSIS_FPS = 6  # face checks per second on the live guide (the video itself runs at camera speed)
ENROLL_SELF_MATCH_MIN = 0.45  # the captured photos must match each other at least this well (cosine)
ENROLL_CAMERA_TIMEOUT_SECONDS = 6  # live guide started but no frames for this long = "Camera is busy"
ENROLL_VIDEO_SIZE = (1280, 720)  # resolution asked from the browser camera
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
ENROLL_MAX_PHOTO_PX = 1600  # dashboard uploads are shrunk to this longest side before saving

# --- Camera workers (started from the dashboard) ----------------------------
WORKER_STATUS_SECONDS = 2.0  # how often a worker writes data/run/<camera>.json
WORKER_STALE_SECONDS = 20.0  # status older than this = "not responding"
WORKER_STOP_TIMEOUT = 10.0  # seconds to wait for a clean stop before killing
WORKER_LOG_MAX_BYTES = 5_000_000  # rotate data/logs/<camera>.log to .log.1 at start when bigger
WORKER_LOG_LINES = 50  # log lines shown per camera
GALLERY_RELOAD_SECONDS = 5.0  # workers reload gallery.npz this often if it changed
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

# --- Live view ------------------------------------------------------------------
LIVE_FRAME_SECONDS = 0.5  # workers write their latest annotated frame at most this often
LIVE_JPEG_QUALITY = 75
LIVE_DECISIONS = 20  # recent decisions kept per camera for the live view
LIVE_STALE_SECONDS = 5.0  # an older frame shows "camera not sending frames"
ENROLL_CAMERA_CLAIM_SECONDS = 600  # the Enrollment page camera switch blocks webcam workers for up to this long

# --- Processing rate -------------------------------------------------------
PROCESS_FPS = 8  # frames per second actually processed (5 to 10 on CPU)

# --- Attendance --------------------------------------------------------------
COOLDOWN_SECONDS = 5 * 60  # ignore the same person on the same camera for this long
SNAPSHOT_SIZE = 160  # longest side of saved face snapshots, in pixels

# --- Dashboard (Streamlit) ----------------------------------------------------
DASHBOARD_HOST = "localhost"  # bind address; only change this behind a firewall/VPN
DASHBOARD_PORT = 8501
DASHBOARD_PASSWORD_ENV = "GATESENSE_ADMIN_PASSWORD"  # admin password: environment variable, or .env
# Optional .env (admin password, camera passwords); real environment variables take priority (see .env.example).
ENV_PATH = Path(os.environ.get("GATESENSE_ENV_FILE") or ROOT_DIR / ".env")
DASHBOARD_USER = "admin"  # name written to the audit log
LIVE_REFRESH_SECONDS = 5  # live events page auto-refresh
LIVE_EVENT_LIMIT = 50  # events shown on the live page

# --- Cameras ---------------------------------------------------------------
# Cameras are normally managed on the dashboard's Camera settings page, which saves them to
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
DEFAULT_CAMERAS = [
    {"name": "entrance", "source": 0, "direction": "IN", "mirror": True},
    # Testing with one webcam: entrance and exit share webcam 0, so run one at a time (two at once is refused).
    # With a second USB webcam, set this to 1 to run both together.
    {"name": "exit", "source": 0, "direction": "OUT", "mirror": True},
    # {"name": "gate-entrance", "direction": "IN",
    #  "source": "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.101:554/Streaming/Channels/101"},
    # {"name": "gate-exit", "direction": "OUT",
    #  "source": "rtsp://gatesense:${GATESENSE_CAM_PASS}@192.168.1.102:554/Streaming/Channels/101"},
]
CAMERAS_FILE = Path(os.environ.get("GATESENSE_CAMERAS_FILE") or ROOT_DIR / "cameras.json")


_cameras_signature = None  # (mtime, size) of the cameras file when CAMERAS was last loaded; None = no file


def _signature(path: Path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def refresh_cameras() -> bool:
    """Reload CAMERAS if CAMERAS_FILE was changed, created or deleted since the last load (the dashboard
    calls this on every page run, so hand edits apply without a restart). True if reloaded."""
    global CAMERAS
    if _signature(CAMERAS_FILE) == _cameras_signature:
        return False
    CAMERAS = load_cameras()
    return True


def load_cameras(path: Path | None = None) -> list[dict]:
    """Cameras from CAMERAS_FILE if it exists (written by the Camera settings page), else DEFAULT_CAMERAS.

    A file that can't be read falls back to the defaults (with a message) rather than stopping the app.
    """
    import json

    global _cameras_signature
    path = path or CAMERAS_FILE
    _cameras_signature = _signature(path)
    if not path.is_file():
        return [dict(c) for c in DEFAULT_CAMERAS]
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["cameras"]
        return [{"name": e["name"], "source": e["source"], "direction": e["direction"],
                 "mirror": bool(e.get("mirror"))} for e in entries]
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"WARNING: could not read {path} ({e}); using DEFAULT_CAMERAS from app/config.py")
        return [dict(c) for c in DEFAULT_CAMERAS]


CAMERAS = load_cameras()
