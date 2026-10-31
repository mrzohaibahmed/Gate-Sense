"""Recognition thresholds, the liveness check and the debounce (cool-down): see and change them. Admin only; every change audited.

Also the Shifts switch (/api/settings/shifts): on = attendance by roster day (shifts, roster), off = by calendar day as
before the shift module. It is kept in the database (app_settings) and read on every request: no restart needed.
Everyone logged in can read it (the dashboard hides the roster when off); only admins change it, with a reason.

And the unknown visitor log switch (/api/settings/unknowns, app/visitors.py): on = the camera workers record faces that
match nobody enrolled, for review; off (the default) = nothing is recorded. Kept in the database too; the workers read
it every UNKNOWN_SWITCH_SECONDS (no restart). Admins and guards can read it (unknowns:read); only admins change it.

The values live in app/config.py; changes are saved to settings.json and applied over them
(config.EDITABLE_SETTINGS lists what can be changed). Camera workers read them when they start.
"""

import json
from typing import Literal

from datetime import datetime

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
    if new["UNKNOWN_MIN_FRAMES"] > new["BEST_FRAMES_PER_TRACK"]:
        raise HTTPException(422, "unknown_min_frames can't be more than best_frames_per_track (the frames kept per "
                                 "face)")
    if new["UNKNOWN_MAX_SCORE"] > new["MATCH_THRESHOLD"]:
        raise HTTPException(422, "unknown_max_score can't be more than match_threshold (a face above it is a match)")
    changes = {key(n): [getattr(config, n), v] for n, v in new.items() if v != getattr(config, n)}
    if changes:
        overrides = {n: v for n, v in new.items() if v != config.DEFAULT_SETTINGS[n]}
        atomic_write_bytes(config.SETTINGS_FILE, (json.dumps(
            {"_comment": "GateSense settings changed through the API; applied over app/config.py. Delete this file "
                         "to go back to the values there.", "settings": overrides}, indent=2) + "\n").encode("utf-8"))
        config.load_settings()
        auth.db_of(request).log_change(admin.username, "change_settings", "settings", changes, body.reason)
    return current(request)


# --- Shifts on / off ----------------------------------------------------------------------------------------------


class ShiftMode(BaseModel):
    enabled: bool = Field(description="on: attendance by roster day; off: by calendar day (first IN, last OUT)")
    updated: datetime | None = Field(description="when it was last switched (None: never, the default)")
    updated_by: str | None


class ShiftModeIn(Reason):
    enabled: bool


@router.get("/shifts", response_model=ShiftMode)
def get_shift_mode(request: Request, _: CurrentUser = Depends(require_permission("account:self"))) -> dict:
    """Are shifts on? Read from the database on every request."""
    return auth.db_of(request).shift_mode()


@router.put("/shifts", response_model=ShiftMode)
def set_shift_mode(body: ShiftModeIn, request: Request, admin: CurrentUser = Depends(manage)) -> dict:
    """Switch shifts on or off (admin only, with a reason; audited enable_shifts / disable_shifts). Nothing stored
    changes: shifts, roster rows and raw events stay, and every screen recalculates from the raw events at once."""
    db = auth.db_of(request)
    db.set_shifts_enabled(body.enabled, admin.username, body.reason)
    return db.shift_mode()


# --- Unknown visitor log on / off -----------------------------------------------------------------------------------


class VisitorLogMode(BaseModel):
    enabled: bool = Field(description="on: the cameras record faces that match nobody enrolled (Unknown visitors)")
    updated: datetime | None = Field(description="when it was last switched (None: never, the default: off)")
    updated_by: str | None


class VisitorLogModeIn(Reason):
    enabled: bool


@router.get("/unknowns", response_model=VisitorLogMode)
def get_unknown_log_mode(request: Request, _: CurrentUser = Depends(require_permission("unknowns:read"))) -> dict:
    """Is the unknown visitor log on? Read from the database on every request."""
    return auth.db_of(request).unknown_log_mode()


@router.put("/unknowns", response_model=VisitorLogMode)
def set_unknown_log_mode(body: VisitorLogModeIn, request: Request, admin: CurrentUser = Depends(manage)) -> dict:
    """Switch the unknown visitor log on or off (admin only, with a reason; audited enable_unknown_log /
    disable_unknown_log). Running cameras follow within UNKNOWN_SWITCH_SECONDS; visits already recorded stay until
    the retention deletes them."""
    db = auth.db_of(request)
    db.set_unknown_log_enabled(body.enabled, admin.username, body.reason)
    return db.unknown_log_mode()
