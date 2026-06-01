"""HTTP API for personas + channel prompts (Plan 29-A).

Two thin CRUD surfaces over the `personas` / `channel_prompts` tables.
The single-default invariant on personas is held by triggers in
`schema.sql`; this layer is responsible for the API-level guardrails
(duplicate name → 409, blank/oversized prompt → 422, deleting the
default persona → 422, unknown channel → 404).

Endpoints (all bearer-gated; the global auth middleware applies):

    GET    /api/personas
    POST   /api/personas
    PUT    /api/personas/{id}
    DELETE /api/personas/{id}

    GET    /api/channels
    PUT    /api/channels/{channel}
    POST   /api/channels/{channel}/reset
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import CHANNEL_REGISTRY
from hermes.repository import channels as channels_repo
from hermes.repository import personas as personas_repo
from hermes.repository.models import ChannelPromptRow, Persona

router = APIRouter(prefix="/api")


def _db(request: Request) -> AsyncEngine:
    return request.app.state.db


def _persona_to_dict(p: Persona) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "prompt": p.prompt,
        "is_default": p.is_default,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _channel_to_dict(row: ChannelPromptRow) -> dict[str, Any]:
    registry = CHANNEL_REGISTRY[row.channel]
    return {
        "channel": row.channel,
        "label": registry["label"],
        "default_prompt": registry["default_prompt"],
        "prompt": row.prompt,
        "is_default_prompt": row.prompt == registry["default_prompt"],
        "default_persona_id": row.default_persona_id,
        "updated_at": row.updated_at,
    }


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------


class PersonaCreate(BaseModel):
    # 1..64 chars matches the "Hermes der Direkte"-class names the user will
    # pick; longer values are almost certainly accidental paste.
    name: str = Field(min_length=1, max_length=64)
    # 8192 is a hard upper bound; the LLM doesn't need more identity text and
    # anything bigger is probably the user dumping a whole document.
    prompt: str = Field(min_length=1, max_length=8192)
    is_default: bool = False


class PersonaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    prompt: str | None = Field(default=None, min_length=1, max_length=8192)
    is_default: bool | None = None


@router.get("/personas")
async def list_personas(request: Request) -> dict[str, Any]:
    rows = await personas_repo.list_all(_db(request))
    return {"personas": [_persona_to_dict(p) for p in rows]}


@router.post("/personas", status_code=status.HTTP_201_CREATED)
async def create_persona(
    body: PersonaCreate, request: Request
) -> dict[str, Any]:
    try:
        persona = await personas_repo.create(
            _db(request),
            name=body.name,
            prompt=body.prompt,
            is_default=body.is_default,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"persona name already exists: {body.name}",
        ) from exc
    return _persona_to_dict(persona)


@router.put("/personas/{persona_id}")
async def update_persona(
    persona_id: int, body: PersonaUpdate, request: Request
) -> dict[str, Any]:
    db = _db(request)
    existing = await personas_repo.get(db, persona_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"persona {persona_id} not found",
        )

    # Refuse to demote the only default — the resolver always needs one.
    if (
        body.is_default is False
        and existing.is_default
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "cannot demote the default persona; set another persona as "
                "default first"
            ),
        )

    try:
        updated = await personas_repo.update(
            db,
            persona_id,
            name=body.name,
            prompt=body.prompt,
            is_default=body.is_default,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"persona name already exists: {body.name}",
        ) from exc
    if updated is None:
        # Race: someone deleted the row between the get and the update.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"persona {persona_id} not found",
        )
    return _persona_to_dict(updated)


@router.delete(
    "/personas/{persona_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_persona(persona_id: int, request: Request) -> Response:
    db = _db(request)
    existing = await personas_repo.get(db, persona_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"persona {persona_id} not found",
        )
    if existing.is_default:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "cannot delete the default persona; set another persona as "
                "default first"
            ),
        )
    deleted = await personas_repo.delete(db, persona_id)
    if not deleted:
        # Race or unexpected — surface as 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"persona {persona_id} not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class ChannelUpdate(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=8192)
    # Explicit None clears the override (the resolver then falls back to
    # the globally-default persona). The repo layer uses a sentinel to
    # distinguish "omitted" from "set to NULL".
    default_persona_id: int | None = None


@router.get("/channels")
async def list_channels(request: Request) -> dict[str, Any]:
    rows = await channels_repo.list_all(_db(request))
    return {"channels": [_channel_to_dict(r) for r in rows]}


@router.put("/channels/{channel}")
async def update_channel(
    channel: str, body: ChannelUpdate, request: Request
) -> dict[str, Any]:
    if channel not in CHANNEL_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown channel: {channel}",
        )
    db = _db(request)

    if body.default_persona_id is not None:
        target = await personas_repo.get(db, body.default_persona_id)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"persona {body.default_persona_id} does not exist",
            )

    # Pydantic collapses "field omitted" and "field: null" into the same
    # None, but the resolver needs to distinguish. `model_fields_set`
    # surfaces only the keys the client actually sent.
    sent = body.model_fields_set
    kwargs: dict[str, Any] = {}
    if "prompt" in sent:
        kwargs["prompt"] = body.prompt
    if "default_persona_id" in sent:
        kwargs["default_persona_id"] = body.default_persona_id

    updated = await channels_repo.update(db, channel, **kwargs)
    if updated is None:
        # Should be unreachable now that we checked CHANNEL_REGISTRY above,
        # but covers the channel-row-missing case explicitly.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"channel row missing: {channel}",
        )
    return _channel_to_dict(updated)


@router.post("/channels/{channel}/reset")
async def reset_channel_prompt(
    channel: str, request: Request
) -> dict[str, Any]:
    if channel not in CHANNEL_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown channel: {channel}",
        )
    reset = await channels_repo.reset_prompt(_db(request), channel)
    if reset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"channel row missing: {channel}",
        )
    return _channel_to_dict(reset)
