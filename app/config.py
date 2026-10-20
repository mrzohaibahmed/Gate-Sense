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
PROVIDERS = ["CPUExecutionProvider"]  # add "CUDAExecutionProvider" later for GPU
CTX_ID = -1  # -1 = CPU, 0 = first GPU

# --- Detection -------------------------------------------------------------
DET_SIZE = (640, 640)  # detector input size
FRAME_WIDTH = 640  # frames are resized to this width before detection
DET_THRESHOLD = 0.5  # minimum detector confidence

# --- Quality filter (applied before embedding) ----------------------------
MIN_FACE_PX = 80  # minimum face box width in pixels
MIN_BLUR_VAR = 60.0  # minimum variance of Laplacian (lower = blurrier)

# --- Recognition -----------------------------------------------------------
EMBEDDING_DIM = 512
MATCH_THRESHOLD = 0.45  # minimum cosine similarity to accept a match
MATCH_MARGIN = 0.05  # top score must beat the second-best person by this much
BEST_FRAMES_PER_TRACK = 5  # recognize only the best N frames per person

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
