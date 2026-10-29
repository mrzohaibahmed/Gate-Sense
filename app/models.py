"""Loads the InsightFace model pack once and shares it, on the device GATESENSE_DEVICE picks.

The detector and recognizer both come from the same buffalo_s pack, so they
share one ``FaceAnalysis`` instance. Providers are always passed explicitly
(``config.CUDA_PROVIDERS`` on an NVIDIA GPU, ``config.CPU_PROVIDERS`` otherwise),
so onnxruntime never picks the Azure provider on its own. A GPU that is missing
or won't load means the CPU, with a warning, never a failed worker; the model
files are the same either way, so embeddings stay comparable with the gallery.
"""

import ctypes
import os
import sys
import threading
from dataclasses import dataclass
from functools import cache

import numpy as np
import onnxruntime
from insightface.app import FaceAnalysis

from app import config

CUDA = "CUDAExecutionProvider"


@dataclass(frozen=True)
class Device:
    name: str  # where the models run: "cuda" or "cpu"
    requested: str  # GATESENSE_DEVICE: auto, cuda or cpu
    warning: str | None = None  # why it isn't the GPU that was asked for (or why the setting was ignored)

    @property
    def providers(self) -> list[str]:
        return list(config.CUDA_PROVIDERS if self.name == "cuda" else config.CPU_PROVIDERS)

    @property
    def ctx_id(self) -> int:
        """InsightFace's prepare(): 0 = the first GPU, -1 = the CPU."""
        return 0 if self.name == "cuda" else -1

    @property
    def label(self) -> str:
        return "NVIDIA GPU (CUDA)" if self.name == "cuda" else "CPU"

    def as_dict(self) -> dict:
        return {"name": self.name, "requested": self.requested, "label": self.label, "providers": self.providers,
                "warning": self.warning}


def resolve_device(requested: str | None = None, available: list[str] | None = None, probe=None) -> Device:
    """The device for `requested` (default GATESENSE_DEVICE): the GPU only if onnxruntime has the CUDA provider and
    `probe()` (default cuda_error) finds it working; otherwise the CPU, with the reason as the warning."""
    requested = config.DEVICE if requested is None else requested
    note = ""
    if requested not in config.DEVICES:
        note = f"GATESENSE_DEVICE={requested!r} isn't auto, cuda or cpu, so auto is used. "
        requested = "auto"
    if requested == "cpu":
        return Device("cpu", requested, note.strip() or None)
    available = onnxruntime.get_available_providers() if available is None else available
    if CUDA not in available:
        return Device("cpu", requested, f"{note}this onnxruntime has no CUDA provider (install onnxruntime-gpu, see "
                                        f"requirements.txt); using the CPU")
    error = (probe or cuda_error)()
    if error:
        return Device("cpu", requested, f"{note}the NVIDIA GPU can't be used ({error}); using the CPU")
    return Device("cuda", requested, note.strip() or None)


def _probe_model() -> bytes:
    """A one-operator ONNX model (y = x + x), enough to see whether a CUDA session starts."""
    from onnx import TensorProto, helper

    x = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1])
    y = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1])
    graph = helper.make_graph([helper.make_node("Add", ["x", "x"], ["y"])], "probe", [x], [y])
    return helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], ir_version=8).SerializeToString()


def _no_driver() -> bool:
    """True when there is clearly no NVIDIA driver (so no usable NVIDIA GPU) on this PC."""
    try:
        if sys.platform == "win32":
            ctypes.WinDLL("nvcuda.dll")
        else:
            ctypes.CDLL("libcuda.so.1")
    except OSError:
        return True
    return False


def cuda_error() -> str | None:
    """None if an onnxruntime session runs on CUDA here; otherwise why not, in plain words."""
    if _no_driver():
        return "no NVIDIA GPU driver found"
    if hasattr(onnxruntime, "preload_dlls"):  # CUDA/cuDNN from the nvidia-* pip packages, if installed
        try:
            onnxruntime.preload_dlls()
        except Exception:  # noqa: BLE001 - then they must come from a CUDA install on the PATH
            pass
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 4  # onnxruntime's own error is long; the reason below says it plainly
    try:
        session = onnxruntime.InferenceSession(_probe_model(), options, providers=config.CUDA_PROVIDERS)
        session.run(None, {"x": np.ones(1, np.float32)})
    except Exception as e:  # noqa: BLE001
        return first_line(e)
    if CUDA not in session.get_providers():
        return "the CUDA provider didn't load: CUDA 12.8+ and cuDNN 9 must be installed"
    return None


def first_line(e: Exception, limit: int = 300) -> str:
    return ((str(e).strip().splitlines() or [type(e).__name__])[0])[:limit]


_device: Device | None = None
_device_lock = threading.Lock()


def device() -> Device:
    """This process's device, resolved once (GATESENSE_DEVICE is read at start, like the other server settings)."""
    global _device
    with _device_lock:
        if _device is None:
            _device = resolve_device()
        return _device


def fall_back_to_cpu(reason: str) -> Device:
    """The GPU failed after all (a model wouldn't load on it): the CPU from now on in this process, with the reason
    as the warning (logged by the caller with the device it ends up on)."""
    global _device
    with _device_lock:
        requested = _device.requested if _device else config.DEVICE
        _device = Device("cpu", requested, f"the NVIDIA GPU can't be used ({reason}); using the CPU")
        return _device


def lost_cuda(sessions) -> bool:
    """True if any of these onnxruntime sessions runs on the CPU only although CUDA was asked for (onnxruntime
    leaves the CUDA provider out on its own when the CUDA libraries are missing)."""
    return any(CUDA not in s.get_providers() for s in sessions)


def onnx_threads(cameras: int | None = None, cores: int | None = None) -> int:
    """Threads per process for detection and recognition: ONNX_THREADS, or (0) the cores shared between the cameras."""
    if config.ONNX_THREADS:
        return config.ONNX_THREADS
    cameras = max(1, len(config.CAMERAS) if cameras is None else cameras)
    return max(1, (cores or os.cpu_count() or 1) // cameras)


def session_options() -> onnxruntime.SessionOptions:
    """onnxruntime options for the face models: onnx_threads() threads, no extra parallel operators."""
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = onnx_threads()
    options.inter_op_num_threads = 1
    return options


def load_face_analysis(dev: Device) -> FaceAnalysis:
    """A prepared FaceAnalysis on `dev` (downloads the pack on first use); not shared, see face_analysis()."""
    fa = FaceAnalysis(
        name=config.MODEL_PACK,
        providers=dev.providers,
        allowed_modules=config.MODEL_MODULES,
        sess_options=session_options(),
    )
    fa.prepare(ctx_id=dev.ctx_id, det_size=config.DET_SIZE, det_thresh=config.DET_THRESHOLD)
    return fa


@cache
def face_analysis() -> FaceAnalysis:
    """Return the shared, prepared FaceAnalysis on device(); on the CPU if it won't load on the GPU."""
    dev = device()
    try:
        fa = load_face_analysis(dev)
    except Exception as e:  # noqa: BLE001 - a CUDA failure mustn't stop the worker: the CPU runs the same model
        if dev.name != "cuda":
            raise
        fall_back_to_cpu(f"the face models didn't load on it: {first_line(e, 200)}")
        return load_face_analysis(device())
    if dev.name == "cuda" and lost_cuda(m.session for m in fa.models.values()):
        fall_back_to_cpu("the face models started on the CPU: CUDA 12.8+ and cuDNN 9 must be installed")
        return load_face_analysis(device())  # sessions with the CPU provider only, as on a PC without a GPU
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
    dev = device()
    model = model_zoo.get_model(str(files[0]), providers=dev.providers, sess_options=session_options())
    model.prepare(ctx_id=dev.ctx_id)
    return model


def threads_in_use() -> dict[str, int]:
    """intra-op threads each loaded model's session actually uses."""
    return {task: model.session.get_session_options().intra_op_num_threads
            for task, model in face_analysis().models.items()}


def providers_in_use() -> dict[str, list[str]]:
    """Execution providers each loaded model's session actually uses."""
    return {task: model.session.get_providers() for task, model in face_analysis().models.items()}
