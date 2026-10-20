"""Central configuration.

All thresholds, frame-rate limits and camera settings live here so they can be
tuned in one place. Other modules import from this file instead of hard-coding
values.
"""

from pathlib import Path

# --- Paths ---------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
KNOWN_DIR = ROOT_DIR / "known"  # known/<person>/*.jpg enrollment photos
DATA_DIR = ROOT_DIR / "data"  # embeddings and database
GALLERY_PATH = DATA_DIR / "gallery.npz"

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
BEST_FRAMES_PER_TRACK = 5  # recognize only the best N frames per person (tracking phase)

# --- Webcam prototype vote (until ByteTrack tracking is added) ---------------
VOTE_FRAMES = 5  # last N decisions for the largest face in view
VOTE_MIN_AGREE = 3  # a name must win this many of them to be confirmed
FACE_LOST_SECONDS = 1.5  # reset the vote after no face for this long

# --- Enrollment ----------------------------------------------------------------
ENROLL_MIN_PHOTOS = 5  # warn when a person has fewer accepted photos
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# --- Processing rate -------------------------------------------------------
PROCESS_FPS = 8  # frames per second actually processed (5 to 10 on CPU)

# --- Attendance --------------------------------------------------------------
DEBOUNCE_SECONDS = 5 * 60  # ignore the same person on the same camera for this long

# --- Cameras ---------------------------------------------------------------
# source: an int for a local webcam index, or an RTSP URL string for an IP camera.
# direction: "IN" for entrance, "OUT" for exit.
CAMERAS = [
    {"name": "webcam", "source": 0, "direction": "IN"},
    # {"name": "gate-entrance", "source": "rtsp://user:pass@192.168.1.10:554/stream1", "direction": "IN"},
    # {"name": "gate-exit", "source": "rtsp://user:pass@192.168.1.11:554/stream1", "direction": "OUT"},
]
