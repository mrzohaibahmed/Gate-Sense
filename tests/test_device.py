"""NVIDIA GPU support: GATESENSE_DEVICE picks the providers, a GPU that is missing or won't load means the CPU with a
warning (never a failed worker), liveness runs where the face models run, and the GPU gives the same embeddings as
the CPU (that last test needs a working NVIDIA GPU and skips without one)."""

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app import config, liveness, models, workers
from tests.test_api import wait_for
from tests.test_data_api import as_role, first_password, items, worker_app  # noqa: F401  (fixtures)
from tests.test_full_resolution import MODEL_DIR, SAMPLE
from tests.test_pipeline import SmallFaceDetector
from tests.test_resilience import run_worker

CUDA = "CUDAExecutionProvider"
CPU_ONLY = ["CPUExecutionProvider"]
WITH_CUDA = [CUDA, "CPUExecutionProvider"]
REAL_MODELS = SAMPLE.is_file() and (MODEL_DIR / "w600k_mbf.onnx").is_file()


def never():
    raise AssertionError("the GPU was tried")


# --- Choosing the device ---------------------------------------------------------------------------------------


def test_cpu_never_tries_the_gpu():
    d = models.resolve_device("cpu", WITH_CUDA, never)
    assert (d.name, d.providers, d.ctx_id, d.warning, d.label) == ("cpu", CPU_ONLY, -1, None, "CPU")


@pytest.mark.parametrize("requested", ["auto", "cuda"])
def test_a_working_gpu_is_used(requested):
    d = models.resolve_device(requested, WITH_CUDA, lambda: None)
    assert (d.name, d.requested, d.providers, d.ctx_id, d.warning) == ("cuda", requested, WITH_CUDA, 0, None)
    assert d.label == "NVIDIA GPU (CUDA)"


@pytest.mark.parametrize("requested", ["auto", "cuda"])
def test_no_cuda_provider_means_the_cpu(requested):
    """The plain onnxruntime package (no CUDA provider at all): the CPU, and the warning says what to install."""
    d = models.resolve_device(requested, CPU_ONLY, never)
    assert (d.name, d.providers, d.ctx_id) == ("cpu", CPU_ONLY, -1)
    assert "install onnxruntime-gpu" in d.warning


@pytest.mark.parametrize("requested", ["auto", "cuda"])
def test_a_gpu_that_fails_means_the_cpu_with_a_warning(requested):
    d = models.resolve_device(requested, WITH_CUDA, lambda: "no NVIDIA GPU driver found")
    assert (d.name, d.requested, d.providers, d.ctx_id) == ("cpu", requested, CPU_ONLY, -1)
    assert d.warning == "the NVIDIA GPU can't be used (no NVIDIA GPU driver found); using the CPU"


def test_an_unknown_setting_is_auto_with_a_warning():
    d = models.resolve_device("gpu", WITH_CUDA, lambda: None)
    assert (d.name, d.requested) == ("cuda", "auto") and "GATESENSE_DEVICE='gpu'" in d.warning


def test_the_setting_comes_from_config(monkeypatch):
    monkeypatch.setattr(config, "DEVICE", "cpu")
    assert models.resolve_device(available=WITH_CUDA, probe=never).requested == "cpu"
    monkeypatch.setattr(models, "_device", None)  # not resolved yet in this process
    assert models.device() == models.Device("cpu", "cpu")


def test_the_real_check_never_raises():
    """On this PC as it is: the GPU if it really works, otherwise the CPU and a reason."""
    d = models.resolve_device("cuda")
    assert d.name in ("cuda", "cpu")
    assert (d.warning is None) == (d.name == "cuda")


# --- Falling back while loading --------------------------------------------------------------------------------


class FakeSession:
    def __init__(self, providers):
        self.providers = list(providers)

    def get_providers(self):
        return self.providers

    def get_inputs(self):
        return [SimpleNamespace(name="input", shape=[1, 3, 80, 80])]


class FakeModel:
    def __init__(self, providers):
        self.session = FakeSession(providers)


def fake_face_analysis(monkeypatch, fail_on_cuda=False, drop_cuda=False):
    """A FaceAnalysis stand-in that records the providers of each load; on CUDA it raises (`fail_on_cuda`) or,
    like onnxruntime without the CUDA libraries, leaves the CUDA provider out (`drop_cuda`)."""
    loads = []

    class FA:
        def __init__(self, name, providers, allowed_modules, sess_options):
            loads.append(list(providers))
            if fail_on_cuda and CUDA in providers:
                raise RuntimeError("CUDA failure 100: no CUDA-capable device is detected\n(file and line)")
            got = [p for p in providers if not (drop_cuda and p == CUDA)]
            self.models = {"detection": FakeModel(got), "recognition": FakeModel(got)}

        def prepare(self, ctx_id, det_size, det_thresh):
            self.ctx_id = ctx_id

    monkeypatch.setattr(models, "FaceAnalysis", FA)
    return loads


@pytest.fixture
def fresh_models():
    models.face_analysis.cache_clear()
    yield
    models.face_analysis.cache_clear()


def test_face_models_load_on_the_gpu(monkeypatch, fresh_models):
    loads = fake_face_analysis(monkeypatch)
    monkeypatch.setattr(models, "_device", models.Device("cuda", "auto"))
    fa = models.face_analysis()
    assert loads == [WITH_CUDA] and fa.ctx_id == 0
    assert models.device() == models.Device("cuda", "auto")


def test_face_models_that_fail_on_the_gpu_load_on_the_cpu(monkeypatch, fresh_models):
    loads = fake_face_analysis(monkeypatch, fail_on_cuda=True)
    monkeypatch.setattr(models, "_device", models.Device("cuda", "cuda"))
    fa = models.face_analysis()
    assert loads == [WITH_CUDA, CPU_ONLY] and fa.ctx_id == -1
    dev = models.device()
    assert (dev.name, dev.requested) == ("cpu", "cuda")
    assert "CUDA failure 100: no CUDA-capable device is detected" in dev.warning
    assert "file and line" not in dev.warning  # the first line only


def test_face_models_onnxruntime_put_on_the_cpu_are_reloaded_there(monkeypatch, fresh_models):
    loads = fake_face_analysis(monkeypatch, drop_cuda=True)
    monkeypatch.setattr(models, "_device", models.Device("cuda", "auto"))
    fa = models.face_analysis()
    assert loads == [WITH_CUDA, CPU_ONLY] and fa.ctx_id == -1
    assert models.device().name == "cpu" and "CUDA 12.8+ and cuDNN 9" in models.device().warning


def test_a_cpu_failure_is_not_hidden(monkeypatch, fresh_models):
    """On the CPU there's nothing to fall back to: a model that won't load is still an error."""
    fake_face_analysis(monkeypatch)
    monkeypatch.setattr(models, "FaceAnalysis", lambda **kw: (_ for _ in ()).throw(RuntimeError("model missing")))
    with pytest.raises(RuntimeError, match="model missing"):
        models.face_analysis()


class LivenessSessions:
    """onnxruntime.InferenceSession stand-in for the liveness models."""

    def __init__(self, fail_on_cuda=False):
        self.fail_on_cuda, self.providers = fail_on_cuda, []

    def __call__(self, path, options, providers):
        self.providers.append(list(providers))
        if self.fail_on_cuda and CUDA in providers:
            raise RuntimeError("CUDNN failure 4: CUDNN_STATUS_INTERNAL_ERROR")
        return FakeSession(providers)


def test_liveness_runs_on_the_face_models_device(monkeypatch, tmp_path):
    import onnxruntime

    sessions = LivenessSessions()
    monkeypatch.setattr(onnxruntime, "InferenceSession", sessions)
    monkeypatch.setattr(models, "_device", models.Device("cuda", "auto"))
    assert liveness.session(tmp_path / "MiniFASNetV2.onnx").get_providers() == WITH_CUDA
    monkeypatch.setattr(models, "_device", models.Device("cpu", "auto"))
    assert liveness.session(tmp_path / "MiniFASNetV2.onnx").get_providers() == CPU_ONLY
    assert sessions.providers == [WITH_CUDA, CPU_ONLY]
    checker = liveness.Liveness([(liveness.session(tmp_path / "a.onnx"), 2.7)])
    assert checker.providers() == CPU_ONLY


def test_liveness_that_fails_on_the_gpu_runs_on_the_cpu(monkeypatch, tmp_path):
    import onnxruntime

    sessions = LivenessSessions(fail_on_cuda=True)
    monkeypatch.setattr(onnxruntime, "InferenceSession", sessions)
    monkeypatch.setattr(models, "_device", models.Device("cuda", "auto"))
    assert liveness.session(tmp_path / "MiniFASNetV2.onnx").get_providers() == CPU_ONLY
    assert sessions.providers == [WITH_CUDA, CPU_ONLY]
    assert models.device().name == "cpu" and "CUDNN_STATUS_INTERNAL_ERROR" in models.device().warning
    # A device passed in (the benchmark's) stays the caller's: the process's device isn't changed by it.
    monkeypatch.setattr(models, "_device", models.Device("cuda", "auto"))
    assert liveness.session(tmp_path / "a.onnx", models.Device("cuda", "cuda")).get_providers() == CPU_ONLY
    assert models.device() == models.Device("cuda", "auto")


@pytest.mark.skipif(not REAL_MODELS, reason="needs the buffalo_s models and InsightFace's sample photo")
def test_real_models_asked_for_the_gpu_without_one_run_on_the_cpu(monkeypatch, fresh_models):
    """GATESENSE_DEVICE=cuda on a PC whose GPU doesn't work: the real models still load (on the CPU, with a
    warning) and give exactly the CPU's embeddings."""
    if models.resolve_device("cuda").name == "cuda":
        pytest.skip("this PC's NVIDIA GPU works (see the GPU/CPU comparison)")
    from app.detector import Detector
    from app.recognizer import Recognizer

    monkeypatch.setattr(models, "_device", models.Device("cuda", "cuda"))  # as if the check had passed
    fa = models.face_analysis()
    assert models.device().name == "cpu" and models.device().warning
    assert all(m.session.get_providers() == CPU_ONLY for m in fa.models.values())
    cpu = models.load_face_analysis(models.Device("cpu", "cpu"))
    photo = cv2.imread(str(SAMPLE))
    face = Detector(cpu).detect(photo)[0]
    assert np.array_equal(Recognizer(fa).embed(photo, face), Recognizer(cpu).embed(photo, face))


@pytest.mark.skipif(not REAL_MODELS, reason="needs the buffalo_s models and InsightFace's sample photo")
def test_gpu_and_cpu_give_the_same_results():
    """The same buffalo_s files on the GPU and on the CPU: the same faces, and embeddings with cosine > 0.99, so a
    gallery enrolled on one matches on the other."""
    gpu = models.resolve_device("cuda")
    if gpu.name != "cuda":
        pytest.skip(f"no usable NVIDIA GPU on this PC: {gpu.warning}")
    from app.detector import Detector
    from app.recognizer import Recognizer

    on_cpu, on_gpu = models.load_face_analysis(models.Device("cpu", "cpu")), models.load_face_analysis(gpu)
    assert all(CUDA in m.session.get_providers() for m in on_gpu.models.values())
    photo = cv2.imread(str(SAMPLE))  # six faces
    cpu_faces, gpu_faces = Detector(on_cpu).detect(photo), Detector(on_gpu).detect(photo)
    assert len(cpu_faces) == len(gpu_faces) == 6
    cpu_rec, gpu_rec = Recognizer(on_cpu), Recognizer(on_gpu)
    for face in cpu_faces:
        twin = min(gpu_faces, key=lambda g: np.abs(g.bbox - face.bbox).sum())
        assert np.abs(twin.bbox - face.bbox).max() < 2  # px
        cpu_emb = cpu_rec.embed(photo, face)
        assert float(cpu_emb @ gpu_rec.embed(photo, face)) > 0.99  # the recognizer alone
        assert float(cpu_emb @ gpu_rec.embed(photo, twin)) > 0.99  # detection and recognition on the GPU


# --- Reported by the workers and on Settings > System --------------------------------------------------------


def test_the_worker_logs_and_reports_its_device(tmp_path, monkeypatch, capsys):
    warning = "the NVIDIA GPU can't be used (no NVIDIA GPU driver found); using the CPU"
    monkeypatch.setattr(models, "_device", models.Device("cpu", "cuda", warning))
    reason, runtime, _writer = run_worker(tmp_path, monkeypatch, SmallFaceDetector(150), frames=4)
    assert reason == "end of video"
    out = capsys.readouterr().out
    assert "Device CPU (GATESENSE_DEVICE=cuda), liveness providers ['CPUExecutionProvider']" in out
    assert f"WARNING: {warning}" in out
    status = json.loads(workers.status_path("gate").read_text(encoding="utf-8"))
    assert status["device"] == models.Device("cpu", "cuda", warning).as_dict()


def test_system_page_shows_the_device(worker_app, monkeypatch):
    a = as_role(worker_app, "admin")
    info = a.get("/api/system").json()
    assert info["device"] == {"active": "cpu", "label": "CPU", "requested": "auto", "providers": CPU_ONLY,
                              "warning": None}
    assert info["camera_devices"] == []
    monkeypatch.setattr(models, "_device", models.Device("cuda", "auto"))
    assert a.get("/api/system").json()["device"]["label"] == "NVIDIA GPU (CUDA)"
    assert a.get("/api/system").json()["device"]["providers"] == WITH_CUDA

    # A running camera's own device, from its status file (the fake worker says CPU, with the reason).
    ids = {c["name"]: c["id"] for c in items(a.get("/api/cameras"))}
    assert a.post(f"/api/cameras/{ids['a']}/start").status_code == 200
    wait_for(lambda: a.get("/api/system").json()["camera_devices"])
    reason = "the NVIDIA GPU can't be used (no NVIDIA GPU driver found); using the CPU"
    assert a.get("/api/system").json()["camera_devices"] == [{"camera": "a", "label": "CPU", "warning": reason}]
    worker = a.get(f"/api/cameras/{ids['a']}").json()["worker"]
    assert (worker["device"], worker["device_warning"]) == ("CPU", reason)
    assert a.post(f"/api/cameras/{ids['a']}/stop").status_code == 200
    assert a.get("/api/system").json()["camera_devices"] == []
