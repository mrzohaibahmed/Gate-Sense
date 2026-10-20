"""Face detection (stub).

Wraps InsightFace's SCRFD detector from the buffalo_s pack on CPU
(onnxruntime). Responsibilities, to be built in the prototype phase:

- Load the model once using settings from ``app.config``
  (``MODEL_PACK``, ``PROVIDERS``, ``DET_SIZE``, ``DET_THRESHOLD``).
- Resize incoming frames to about ``FRAME_WIDTH`` pixels wide.
- Return each face's bounding box, detector score and 5 landmarks
  (eyes, nose, mouth corners), scaled back to the original frame.
- Apply the quality filter: minimum face width (``MIN_FACE_PX``),
  blur check (variance of Laplacian vs ``MIN_BLUR_VAR``), and a rough
  frontal-pose check from the landmarks.
"""
