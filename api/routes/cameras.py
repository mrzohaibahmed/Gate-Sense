"""Cameras and their workers (the Phase 1 WorkerManager): status for cameras:read, start/stop for cameras:control."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api import auth
from api.auth import CurrentUser, require_permission
from api.workers import CameraBusy, WorkerInfo
from app.workers import WorkerError

router = APIRouter(prefix="/api/cameras", tags=["cameras"], responses=auth.PROTECTED_RESPONSES)
control = require_permission("cameras:control")


class Camera(WorkerInfo):
    direction: str


def manager(request: Request):
    return request.app.state.workers


@router.get("", response_model=list[Camera])
def list_cameras(request: Request, _: CurrentUser = Depends(require_permission("cameras:read"))) -> list[Camera]:
    m = manager(request)
    directions = {c["name"]: c["direction"] for c in m.cameras}
    return [Camera(**info.model_dump(), direction=directions.get(info.camera, "")) for info in m.statuses()]


def _known(request: Request, name: str) -> None:
    if name not in {c["name"] for c in manager(request).cameras}:
        raise HTTPException(404, f"No camera named {name!r}.")


@router.post("/{name}/start", response_model=WorkerInfo)
def start(name: str, request: Request, user: CurrentUser = Depends(control)) -> WorkerInfo:
    _known(request, name)
    try:
        info = manager(request).start(name)
    except CameraBusy as e:
        raise HTTPException(409, str(e)) from None
    except WorkerError as e:
        raise HTTPException(400, str(e)) from None
    auth.db_of(request).audit(user.username, "start_camera", f"camera:{name}", {"pid": info.pid}, "started from the API")
    return info


@router.post("/{name}/stop")
def stop(name: str, request: Request, user: CurrentUser = Depends(control)) -> dict:
    _known(request, name)
    result = manager(request).stop(name)
    if result != "not running":
        auth.db_of(request).audit(user.username, "stop_camera", f"camera:{name}", {"result": result},
                                  "stopped from the API")
    return {"camera": name, "result": result}
