"""HTTP API for personas + channel prompts (Plan 29-A → 36 → 37).

Two thin CRUD surfaces over the `personas` / `channel_prompts` tables.
The per-persona skill-activation layer (Plan 33) was dropped in Plan 37
in favour of the global catalog-index + `skill_load` tool pattern.
The single-default invariant on personas is held in the repo layer;
this layer is responsible for the API-level guardrails
(duplicate name → 409, blank/oversized prompt → 422, deleting the
default persona → 422, unknown channel → 404).

Endpoints (all bearer-gated; the global auth middleware applies):

    GET    /api/personas
    POST   /api/personas
    PUT    /api/personas/{id}
    DELETE /api/personas/{id}

    GET    /api/personas/{id}/history                       (Plan 36)
    POST   /api/personas/{id}/history/{snapshot_id}/restore (Plan 36)

    GET    /api/channels
    PUT    /api/channels/{channel}
    POST   /api/channels/{channel}/reset

This package was split out of a single 631-LoC `preferences.py` for the
>500-LoC rule; the public surface is unchanged — `main.py` still imports
`router` from `hermes.routes.preferences`."""

from __future__ import annotations

from fastapi import APIRouter

from . import channels, persona_history, personas
from ._models import _channel_to_dict, _db, _history_to_dict, _persona_to_dict

router = APIRouter(prefix="/api")
router.include_router(personas.router)
router.include_router(persona_history.router)
router.include_router(channels.router)

__all__ = [
    "_channel_to_dict",
    "_db",
    "_history_to_dict",
    "_persona_to_dict",
    "router",
]
