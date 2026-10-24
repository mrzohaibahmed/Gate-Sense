"""Recognition thresholds, the liveness check and the debounce (cool-down): see and change them. Admin only; every change audited.

The values live in app/config.py; changes are saved to settings.json and applied over them
(config.EDITABLE_SETTINGS lists what can be changed). Camera workers read them when they start.
"""

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api import auth
from api.auth import CurrentUser, require_permission
from api.schemas import Reason
from app import config
from app.fileutil import atomic_write_bytes

router = APIRouter(prefix="/api/settings", tags=["settings"], responses=auth.PROTECTED_RESPONSES)
manage = require_permission("settings:manage")
APPLIES = "Camera workers read these when they start: restart running cameras to apply a change."


def key(name: str) -> str:
    return name.lower()


class Setting(BaseModel):
    key: str = Field(description="e.g. match_threshold (config.MATCH_THRESHOLD)")
    value: float = Field(description="for a bool setting: 1 = on, 0 = off")
    default: float = Field(description="the value in app/config.py")
    type: Literal["int", "float", "bool"]
    minimum: float
    maximum: float
    description: str
    changed: bool = Field(description="differs from app/config.py")


class Settings(BaseModel):
    settings: list[Setting]
    applies: str
    running_cameras: list[str] = Field(description="cameras to restart for a change to take effect")


class SettingsPatch(Reason):
    values: dict[str, float | bool] = Field(description='{"match_threshold": 0.5, "cooldown_seconds": 600, '
                                                    '"liveness_enabled": false, ...} (bool settings: true/false '
                                                    'or 1/0)')


def current(request: Request) -> Settings:
    config.refresh_settings()
    items = []
    for name, (kind, low, high, meaning) in config.EDITABLE_SETTINGS.items():
        value, default = getattr(config, name), config.DEFAULT_SETTINGS[name]
        items.append(Setting(key=key(name), value=float(value), default=float(default), type=kind.__name__,
                             minimum=low, maximum=high, description=meaning, changed=value != default))
    running = [w.camera for w in request.app.state.workers.statuses() if w.state != "stopped"]
    return Settings(settings=items, applies=APPLIES, running_cameras=running)


@router.get("", response_model=Settings)
def get_settings(request: Request, _: CurrentUser = Depends(manage)) -> Settings:
    return current(request)


@router.patch("", response_model=Settings)
def update_settings(body: SettingsPatch, request: Request, admin: CurrentUser = Depends(manage)) -> Settings:
    """Change one or more settings. Each change goes to the audit log with its old and new value."""
    config.refresh_settings()
    names = {key(n): n for n in config.EDITABLE_SETTINGS}
    new = {n: getattr(config, n) for n in config.EDITABLE_SETTINGS}
    for k, value in body.values.items():
        if k not in names:
            raise HTTPException(422, f"{k} can't be changed here; use one of: {', '.join(sorted(names))}")
        try:
            new[names[k]] = config.check_setting(names[k], value)
        except ValueError as e:
            raise HTTPException(422, str(e).replace(names[k], k)) from None
    if new["VOTE_MIN_AGREE"] > new["BEST_FRAMES_PER_TRACK"]:
        raise HTTPException(422, "vote_min_agree can't be more than best_frames_per_track")
    changes = {key(n): [getattr(config, n), v] for n, v in new.items() if v != getattr(config, n)}
    if changes:
        overrides = {n: v for n, v in new.items() if v != config.DEFAULT_SETTINGS[n]}
        atomic_write_bytes(config.SETTINGS_FILE, (json.dumps(
            {"_comment": "GateSense settings changed through the API; applied over app/config.py. Delete this file "
                         "to go back to the values there.", "settings": overrides}, indent=2) + "\n").encode("utf-8"))
        config.load_settings()
        auth.db_of(request).log_change(admin.username, "change_settings", "settings", changes, body.reason)
    return current(request)
