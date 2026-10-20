"""Face recognition.

Turns a detected face into a 512-d ArcFace embedding using the buffalo_s
recognition model (MobileFaceNet). The face is aligned to 112x112 from its 5
landmarks first. Embeddings are L2-normalized, so cosine similarity is a plain
dot product. No training: embeddings come straight from the pretrained model.
"""

import warnings

import numpy as np
from insightface.utils import face_align

from app.detector import Detection

# insightface 2.1's alignment calls a scikit-image API deprecated in 0.26; harmless until skimage 2.2.
warnings.filterwarnings("ignore", message="`estimate` is deprecated", category=FutureWarning)


class Recognizer:
    def __init__(self, fa=None):
        if fa is None:
            from app.models import face_analysis

            fa = face_analysis()
        self.model = fa.models["recognition"]

    def embed(self, img: np.ndarray, det: Detection) -> np.ndarray:
        """Return the normalized 512-d embedding of one detected face."""
        aligned = face_align.norm_crop(img, landmark=det.kps, image_size=self.model.input_size[0])
        emb = self.model.get_feat(aligned).flatten().astype(np.float32)
        return emb / np.linalg.norm(emb)
