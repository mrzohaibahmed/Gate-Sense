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
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
ENROLL_MAX_PHOTO_PX = 1600  # dashboard uploads are shrunk to this longest side before saving

# --- Camera workers (started from the dashboard) ----------------------------
WORKER_STATUS_SECONDS = 2.0  # how often a worker writes data/run/<camera>.json
WORKER_STALE_SECONDS = 20.0  # status older than this = "not responding"
WORKER_STOP_TIMEOUT = 10.0  # seconds to wait for a clean stop before killing
WORKER_LOG_MAX_BYTES = 5_000_000  # rotate data/logs/<camera>.log to .log.1 at start when bigger
WORKER_LOG_LINES = 50  # log lines shown per camera
GALLERY_RELOAD_SECONDS = 5.0  # workers reload gallery.npz this often if it changed

# --- Processing rate -------------------------------------------------------
PROCESS_FPS = 8  # frames per second actually processed (5 to 10 on CPU)

# --- Attendance --------------------------------------------------------------
COOLDOWN_SECONDS = 5 * 60  # ignore the same person on the same camera for this long
SNAPSHOT_SIZE = 160  # longest side of saved face snapshots, in pixels

# --- Dashboard (Streamlit) ----------------------------------------------------
DASHBOARD_HOST = "localhost"  # bind address; only change this behind a firewall/VPN
DASHBOARD_PORT = 8501
DASHBOARD_PASSWORD_ENV = "GATESENSE_ADMIN_PASSWORD"  # admin password: environment variable, or .env
ENV_PATH = ROOT_DIR / ".env"  # optional; real environment variables take priority (see .env.example)
DASHBOARD_USER = "admin"  # name written to the audit log
LIVE_REFRESH_SECONDS = 5  # live events page auto-refresh
LIVE_EVENT_LIMIT = 50  # events shown on the live page

# --- Cameras ---------------------------------------------------------------
# Run one recognize.py per camera: --camera <name>. The first entry is the default.
# source: an int for a local webcam index, a video file path, or an RTSP URL.
# direction: "IN" for entrance, "OUT" for exit.
CAMERAS = [
    {"name": "entrance", "source": 0, "direction": "IN"},
    {"name": "exit", "source": str(DATA_DIR / "clips" / "exit.mp4"), "direction": "OUT"},
    # {"name": "gate-entrance", "source": "rtsp://user:pass@192.168.1.10:554/stream1", "direction": "IN"},
    # {"name": "gate-exit", "source": "rtsp://user:pass@192.168.1.11:554/stream1", "direction": "OUT"},
]
