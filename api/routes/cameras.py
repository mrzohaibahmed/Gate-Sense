"""Cameras: the list (with each worker's status), add, change, start / stop, and a connection test.

The camera list is cameras.json (app/camera_settings.py, the same as the dashboard's Camera settings page); a
camera's id, site and events are in the cameras table. Passwords never appear in a response: sources keep their
${VAR} placeholders (a typed-in password would be masked), and a Hikvision password sent with a change goes
straight to .env (password_set tells whether one is there).
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.paging import ListParams, Page, list_params, paginate
from api.schemas import Direction
from api.workers import CameraBusy, WorkerInfo
from app import camera_settings, sources
from app import workers as app_workers
from app.camera import is_webcam, mask_source, parse_source
from app.workers import WorkerError

router = APIRouter(prefix="/api/cameras", tags=["cameras"], responses=auth.PROTECTED_RESPONSES)
read = require_permission("cameras:read")
control = require_permission("cameras:control")
manage = require_permission("cameras:manage")
API_REASON = "changed through the API"
CameraType = Literal["webcam", "hikvision", "rtsp", "file"]


class Roi(BaseModel):
    """Region of interest, as fractions of the frame. Stored with the camera; the worker doesn't apply it yet."""
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


class Camera(BaseModel):
    id: int
    position: int = Field(description="order in the camera list (the first is recognize.py's default)")
    name: str
    site_id: int | None
    site: str | None
    direction: Direction
    type: CameraType
    mirror: bool
    source: str | int = Field(description="webcam index, file path or RTSP URL with ${VAR} placeholders; "
                                          "never a password")
    roi: Roi | None
    index: int | None = Field(None, description="webcam")
    ip: str | None = Field(None, description="hikvision")
    port: int | None = Field(None, description="hikvision")
    user: str | None = Field(None, description="hikvision camera user name")
    channel: int | None = Field(None, description="hikvision")
    stream: Literal["main", "sub"] | None = Field(None, description="hikvision")
    path: str | None = Field(None, description="file")
    password_variable: str | None = Field(None, description="hikvision: the .env variable holding the password")
    password_set: bool | None = Field(None, description="hikvision: is that variable set")
    missing_variables: list[str] = Field(description="${VAR}s in the source that aren't set (the camera can't start)")
    worker: WorkerInfo


class CameraFields(BaseModel):
    direction: Direction | None = None
    type: CameraType | None = None
    mirror: bool | None = None
    site_id: int | None = None
    roi: Roi | None = None
    index: int | None = Field(None, description="webcam: 0 is the first")
    ip: str | None = Field(None, description="hikvision")
    port: int | None = Field(None, description="hikvision (default 554)")
    user: str | None = Field(None, description="hikvision")
    channel: int | None = Field(None, description="hikvision (default 1)")
    stream: Literal["main", "sub"] | None = Field(None, description="hikvision (default main)")
    url: str | None = Field(None, description="rtsp: the password as ${VARIABLE}, never typed in")
    path: str | None = Field(None, description="file")
    password: str | None = Field(None, description="hikvision only: saved to .env, never returned")


class CameraIn(CameraFields):
    name: str = Field(description="letters, digits, '-' or '_'; can't be changed later")
    direction: Direction
    type: CameraType
    reason: str = API_REASON


class CameraPatch(CameraFields):
    reason: str = API_REASON


class CameraTest(CameraFields):
    camera_id: int | None = Field(None, description="test a saved camera (it must be stopped); otherwise give the "
                                                    "camera fields as for adding one")
    timeout_seconds: float = Field(12, ge=1, le=30)


class TestResult(BaseModel):
    ok: bool
    width: int | None
    height: int | None
    fps: float | None = Field(description="what the camera or file reports (None if it doesn't say)")
    codec: str | None
    error: str | None = Field(description="why there was no picture (passwords masked)")


class StopResult(BaseModel):
    camera: str
    result: str = Field(description='"stopped cleanly", "not running", or what happened instead')


def cameras(request: Request) -> list[dict]:
    """cameras.json entries with their table row (id, site) and worker status, in list order."""
    manager = request.app.state.workers
    table = {c["name"]: c for c in auth.db_of(request).cameras(manager.cameras)}
    statuses = {w.camera: w for w in manager.statuses()}
    out = []
    for position, e in enumerate(camera_settings.entries()):
        row = table.get(e["name"])
        if row is None or e["name"] not in statuses:
            continue
        source = e["source"]
        var = camera_settings.password_var(e["name"]) if e.get("type") == "hikvision" else None
        out.append({
            **{k: e.get(k) for k in ("index", "ip", "port", "user", "channel", "stream", "path")},
            "id": row["id"], "position": position, "name": e["name"], "site_id": row["site_id"], "site": row["site"],
            "direction": e["direction"], "type": e.get("type") or "file", "mirror": bool(e.get("mirror")),
            "source": camera_settings.public_source(source), "roi": e.get("roi"), "password_variable": var,
            "password_set": None if var is None else bool(camera_settings.password_status(var)),
            "missing_variables": sources.missing_variables(source), "worker": statuses[e["name"]],
            "state": statuses[e["name"]].state,
        })
    return out


def get_or_404(request: Request, camera_id: int) -> dict:
    cam = next((c for c in cameras(request) if c["id"] == camera_id), None)
    if cam is None:
        raise HTTPException(404, f"No camera with id {camera_id}.")
    return cam


def running(request: Request) -> set[str]:
    return {w.camera for w in request.app.state.workers.statuses() if w.state != "stopped"}


def entry_of(name: str) -> dict:
    return dict(next(e for e in camera_settings.entries() if e["name"] == name))


def fields(body: CameraFields) -> dict:
    """The camera fields that were sent (password, site, reason and test options are handled separately)."""
    sent = body.model_dump(include=body.model_fields_set - {"password", "site_id", "reason", "camera_id",
                                                             "timeout_seconds"})
    if sent.get("roi") is not None:
        sent["roi"] = dict(sent["roi"])
    return sent


def save(request: Request, entry: dict, body: CameraFields, user: CurrentUser) -> None:
    try:
        with auth.db_of(request).conn() as conn:
            camera_settings.upsert(entry, user.username, conn, running(request), body.password, body.reason)
    except ValueError as e:
        raise HTTPException(409 if "is running" in str(e) else 422, mask_source(str(e)).capitalize()) from None


def set_site(request: Request, name: str, site_id: int | None, user: CurrentUser, reason: str) -> None:
    db = auth.db_of(request)
    if site_id is None:
        return
    if db.site(site_id) is None:
        raise HTTPException(422, f"No site with id {site_id}.")
    current = next(c for c in db.cameras(request.app.state.workers.cameras) if c["name"] == name)
    if current["site_id"] != site_id:
        entry = entry_of(name)
        db.set_camera_row(name, entry["source"], entry["direction"], site_id)
        db.log_change(user.username, "set_camera_site", f"camera:{name}", {"site_id": [current["site_id"], site_id]},
                      reason)


@router.get("", response_model=Page[Camera])
def list_cameras(request: Request, params: ListParams = Depends(list_params), _: CurrentUser = Depends(read)) -> dict:
    """Every configured camera with its worker's status (state, fps, last frame, last error). Default order: the
    camera list's."""
    items = [c for c in cameras(request) if params.site_id is None or c["site_id"] == params.site_id]
    return paginate(items, params, ("name", "type", "direction", "site"),
                    ("position", "id", "name", "direction", "type", "site", "state"), "position")


@router.get("/{camera_id}", response_model=Camera)
def get_camera(camera_id: int, request: Request, _: CurrentUser = Depends(read)) -> dict:
    return get_or_404(request, camera_id)


@router.post("", response_model=Camera, status_code=201)
def create_camera(body: CameraIn, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Add a camera. For a Hikvision camera, send its password in `password`: it goes to .env, never into a
    response or cameras.json. For other RTSP URLs write the password as ${VARIABLE} and set it in .env."""
    if any(e["name"].lower() == body.name.strip().lower() for e in camera_settings.entries()):
        raise HTTPException(409, f"There is already a camera called {body.name.strip()!r}.")
    if body.site_id is not None and auth.db_of(request).site(body.site_id) is None:
        raise HTTPException(422, f"No site with id {body.site_id}.")
    save(request, {**fields(body), "name": body.name.strip()}, body, user)
    built = entry_of(body.name.strip())
    camera_id = auth.db_of(request).set_camera_row(built["name"], built["source"], built["direction"], None)
    set_site(request, built["name"], body.site_id, user, body.reason)
    return get_or_404(request, camera_id)


@router.patch("/{camera_id}", response_model=Camera)
def update_camera(camera_id: int, body: CameraPatch, request: Request, user: CurrentUser = Depends(manage)) -> dict:
    """Change a camera (fields left out stay; the name can't change). A running camera can only be moved to another
    site; stop it to change anything else. roi: null removes the region."""
    cam = get_or_404(request, camera_id)
    changed = fields(body)
    if changed or body.password:
        save(request, {**entry_of(cam["name"]), **changed}, body, user)
        built = entry_of(cam["name"])
        auth.db_of(request).set_camera_row(built["name"], built["source"], built["direction"], None)
    set_site(request, cam["name"], body.site_id, user, body.reason)
    return get_or_404(request, camera_id)


@router.post("/{camera_id}/start", response_model=WorkerInfo)
def start_camera(camera_id: int, request: Request, user: CurrentUser = Depends(control)) -> WorkerInfo:
    """Start the camera's worker. 409 if any process (this API, the dashboard, the command line) already runs it
    or its webcam."""
    name = get_or_404(request, camera_id)["name"]
    try:
        info = request.app.state.workers.start(name)
    except CameraBusy as e:
        raise HTTPException(409, str(e)) from None
    except WorkerError as e:
        raise HTTPException(400, str(e)) from None
    auth.db_of(request).audit(user.username, "start_camera", f"camera:{name}", {"pid": info.pid}, "started from the API")
    return info


@router.post("/{camera_id}/stop", response_model=StopResult)
def stop_camera(camera_id: int, request: Request, user: CurrentUser = Depends(control)) -> StopResult:
    name = get_or_404(request, camera_id)["name"]
    result = request.app.state.workers.stop(name)
    if result != "not running":
        auth.db_of(request).audit(user.username, "stop_camera", f"camera:{name}", {"result": result},
                                  "stopped from the API")
    return StopResult(camera=name, result=result)


@router.post("/test", response_model=TestResult)
def test_camera(body: CameraTest, request: Request, _: CurrentUser = Depends(manage)) -> TestResult:
    """Open a source once and report its resolution, fps and codec. A saved camera must be stopped (a camera is
    never opened twice); a webcam in use by a worker or the Enrollment camera is refused. A Hikvision `password`
    is used for this test only."""
    manager = request.app.state.workers
    if body.camera_id is not None:
        cam = get_or_404(request, body.camera_id)
        if cam["state"] != "stopped":
            raise HTTPException(409, f"{cam['name']} is running: stop it first (a camera is never opened twice).")
        entry = entry_of(cam["name"])
    else:
        try:
            entry = camera_settings.build({**fields(body), "name": "connection-test"}, [])
        except ValueError as e:
            raise HTTPException(422, mask_source(str(e)).capitalize()) from None
    name, source = entry["name"], entry["source"]
    if is_webcam(parse_source(str(source))):
        if other := app_workers.webcam_conflict(name, source, manager.cameras, manager.run_dir):
            raise HTTPException(409, f"Webcam {source} is in use by the {other} worker; stop it first.")
        if app_workers.enrollment_camera_claimed(manager.run_dir):
            raise HTTPException(409, f"Webcam {source} is in use by the dashboard's Enrollment camera.")
    extra = None
    if body.password and entry["type"] == "hikvision":
        extra = {camera_settings.password_var(name): body.password}
    result = camera_settings.test_connection(source, timeout=body.timeout_seconds, extra=extra)
    return TestResult(ok=result["ok"], width=result["width"], height=result["height"], fps=result["fps"],
                      codec=result["codec"], error=mask_source(result["error"]) if result["error"] else None)
