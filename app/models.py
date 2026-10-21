"""Loads the InsightFace model pack once and shares it.

The detector and recognizer both come from the same buffalo_s pack, so they
share one ``FaceAnalysis`` instance. Providers are passed explicitly from
``app.config.PROVIDERS`` (CPU only), so onnxruntime never picks the Azure
provider or a GPU on its own.
"""

from functools import cache

from insightface.app import FaceAnalysis

from app import config


@cache
def face_analysis() -> FaceAnalysis:
    """Return the shared, prepared FaceAnalysis (downloads the pack on first use)."""
    fa = FaceAnalysis(
        name=config.MODEL_PACK,
        providers=config.PROVIDERS,
        allowed_modules=config.MODEL_MODULES,
    )
    fa.prepare(ctx_id=config.CTX_ID, det_size=config.DET_SIZE, det_thresh=config.DET_THRESHOLD)
    return fa


@cache
def pose_model():
    """InsightFace's 3D-landmark model from the same pack (1k3d68.onnx), which also gives head pose.

    Loaded only for enrollment photo checks (it's large); the live cameras never use it. None if missing.
    """
    from pathlib import Path

    from insightface.model_zoo import model_zoo

    folder = Path(face_analysis().model_dir)
    files = sorted(folder.glob("1k3d68*.onnx")) or sorted(folder.glob("*3d68*.onnx"))
    if not files:
        return None
    model = model_zoo.get_model(str(files[0]), providers=config.PROVIDERS)
    model.prepare(ctx_id=config.CTX_ID)
    return model


def providers_in_use() -> dict[str, list[str]]:
    """Execution providers each loaded model's session actually uses."""
    return {task: model.session.get_providers() for task, model in face_analysis().models.items()}
