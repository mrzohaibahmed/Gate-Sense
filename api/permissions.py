"""Roles and what each may do: the one place to change API access.

Every endpoint except login and the health check is protected with
``api.auth.require_permission(<permission>)``; a role gets an endpoint only if
the permission is in its set below. Permissions not used by any endpoint yet
are for later phases (live view, reports export).
"""

from typing import Literal

ROLES = ("admin", "hr", "viewer", "guard")
Role = Literal[ROLES]  # for request bodies: validated, and listed in /docs

PERMISSIONS = {
    "account:self": "see your own account, change your password, log out",
    "users:manage": "list, create and change user accounts",
    "cameras:read": "see the cameras and their workers' status",
    "cameras:control": "start and stop camera workers",
    "cameras:manage": "add and change cameras, test a camera connection",
    "sites:read": "see the list of sites (to filter by site)",
    "sites:manage": "add, change and delete sites",
    "settings:manage": "see and change recognition thresholds and the debounce (cool-down)",
    "live:view": "watch the live camera view",
    "live:decisions": "see every recognition decision live (scores, margins, skip reasons): for tuning",
    "attendance:read": "see attendance events and daily summaries",
    "attendance:correct": "add manual events, change times, void and restore events",
    "people:read": "see enrolled people",
    "people:manage": "add and edit people, their photos and face data",
    "reports:read": "see and export daily and monthly reports",
    "audit:read": "see the audit log",
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(PERMISSIONS),
    "hr": frozenset({
        "account:self", "sites:read", "cameras:read", "live:view", "attendance:read", "attendance:correct",
        "people:read", "people:manage", "reports:read", "audit:read",
    }),
    "viewer": frozenset({"account:self", "sites:read", "cameras:read", "attendance:read", "people:read",
                         "reports:read"}),
    "guard": frozenset({"account:self", "sites:read", "cameras:read", "cameras:control", "live:view",
                        "attendance:read"}),
}

assert set(ROLE_PERMISSIONS) == set(ROLES)
assert all(perms <= set(PERMISSIONS) for perms in ROLE_PERMISSIONS.values())


def allowed(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
