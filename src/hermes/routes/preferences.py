"""HTTP API for personas + channel prompts (Plan 29-A) + persona-skill
activation (Plan 33).

Three thin CRUD surfaces over the `personas` / `channel_prompts` /
`persona_skills` tables. The single-default invariant on personas is
held by triggers in `schema.sql`; this layer is responsible for the
API-level guardrails (duplicate name → 409, blank/oversized prompt →
422, deleting the default persona → 422, unknown channel → 404,
unknown skill in persona-skill set → 422).

Endpoints (all bearer-gated; the global auth middleware applies):

    GET    /api/personas
    POST   /api/personas
    PUT    /api/personas/{id}
    DELETE /api/personas/{id}

    GET    /api/personas/{id}/skills
    PUT    /api/personas/{id}/skills

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

from hermes.errors import ErrorCode
from hermes.personas import CHANNEL_REGISTRY
from hermes.repository import channels as channels_repo
from hermes.repository import personas as personas_repo
from hermes.repository import skills as skills_repo
from hermes.repository.models import ChannelPromptRow, Persona, Skill
from hermes.routes.skills import SkillResponse

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Response models — kept Pydantic so OpenAPI emits proper TS types for
# `pnpm run gen:api`. The repo layer returns dataclasses; conversion is
# trivial and lives in `_persona_to_dict` / `_channel_to_dict` below.
# ---------------------------------------------------------------------------


class PersonaResponse(BaseModel):
    id: int
    name: str
    prompt: str
    is_default: bool
    created_at: int
    updated_at: int


class PersonaListResponse(BaseModel):
    personas: list[PersonaResponse]


class ChannelPromptResponse(BaseModel):
    channel: str
    label: str
    default_prompt: str
    prompt: str
    # True iff `prompt == default_prompt` — the UI uses this to decide
    # whether to render the "Reset prompt" button without doing the
    # comparison client-side.
    is_default_prompt: bool
    default_persona_id: int | None
    updated_at: int


class ChannelPromptListResponse(BaseModel):
    channels: list[ChannelPromptResponse]


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


@router.get("/personas", response_model=PersonaListResponse)
async def list_personas(request: Request) -> dict[str, Any]:
    rows = await personas_repo.list_all(_db(request))
    return {"personas": [_persona_to_dict(p) for p in rows]}


@router.post(
    "/personas",
    status_code=status.HTTP_201_CREATED,
    response_model=PersonaResponse,
)
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
            detail={
                "code": ErrorCode.PERSONA_NAME_CONFLICT.value,
                "params": {"name": body.name},
            },
        ) from exc
    return _persona_to_dict(persona)


@router.put("/personas/{persona_id}", response_model=PersonaResponse)
async def update_persona(
    persona_id: int, body: PersonaUpdate, request: Request
) -> dict[str, Any]:
    db = _db(request)
    existing = await personas_repo.get(db, persona_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.PERSONA_NOT_FOUND.value,
                "params": {"id": persona_id},
            },
        )

    # Refuse to demote the only default — the resolver always needs one.
    if (
        body.is_default is False
        and existing.is_default
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=ErrorCode.PERSONA_DEFAULT_DEMOTE.value,
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
            detail={
                "code": ErrorCode.PERSONA_NAME_CONFLICT.value,
                "params": {"name": body.name or ""},
            },
        ) from exc
    if updated is None:
        # Race: someone deleted the row between the get and the update.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.PERSONA_NOT_FOUND.value,
                "params": {"id": persona_id},
            },
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
            detail={
                "code": ErrorCode.PERSONA_NOT_FOUND.value,
                "params": {"id": persona_id},
            },
        )
    if existing.is_default:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=ErrorCode.PERSONA_DEFAULT_DELETE.value,
        )
    deleted = await personas_repo.delete(db, persona_id)
    if not deleted:
        # Race or unexpected — surface as 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.PERSONA_NOT_FOUND.value,
                "params": {"id": persona_id},
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Persona-skill activation (Plan 33)
# ---------------------------------------------------------------------------


class PersonaSkillItem(BaseModel):
    """One row in the persona's skill list as returned by GET — the full
    skill payload is embedded so the UI can render the list without a
    second round-trip to /api/skills."""

    skill: SkillResponse  # forward-declared below; FastAPI resolves at import.
    ordering: int
    enabled: bool


class PersonaSkillListResponse(BaseModel):
    skills: list[PersonaSkillItem]


class PersonaSkillSetItem(BaseModel):
    """One row in the PUT body — refers to a skill by id, no inline data."""

    skill_id: int
    ordering: int = Field(ge=0)
    enabled: bool


class PersonaSkillSetRequest(BaseModel):
    items: list[PersonaSkillSetItem]


def _skill_to_response_dict(s: Skill) -> dict[str, Any]:
    return {
        "id": s.id,
        "slug": s.slug,
        "name": s.name,
        "description": s.description,
        "when_to_use": s.when_to_use,
        "body_markdown": s.body_markdown,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


async def _persona_skills_payload(
    db: AsyncEngine, persona_id: int
) -> dict[str, Any]:
    rows = await skills_repo.list_for_persona(db, persona_id)
    return {
        "skills": [
            {
                "skill": _skill_to_response_dict(skill),
                "ordering": ordering,
                "enabled": enabled,
            }
            for skill, ordering, enabled in rows
        ]
    }


@router.get(
    "/personas/{persona_id}/skills",
    response_model=PersonaSkillListResponse,
)
async def list_persona_skills(
    persona_id: int, request: Request
) -> dict[str, Any]:
    db = _db(request)
    persona = await personas_repo.get(db, persona_id)
    if persona is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.PERSONA_NOT_FOUND.value,
                "params": {"id": persona_id},
            },
        )
    return await _persona_skills_payload(db, persona_id)


@router.put(
    "/personas/{persona_id}/skills",
    response_model=PersonaSkillListResponse,
)
async def set_persona_skills(
    persona_id: int, body: PersonaSkillSetRequest, request: Request
) -> dict[str, Any]:
    db = _db(request)
    persona = await personas_repo.get(db, persona_id)
    if persona is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.PERSONA_NOT_FOUND.value,
                "params": {"id": persona_id},
            },
        )

    items = [item.model_dump() for item in body.items]
    try:
        await skills_repo.set_persona_skills(db, persona_id, items)
    except IntegrityError as exc:
        # Most likely cause: skill_id doesn't exist (FK violation) or a
        # duplicate (skill_id) within the same items list (PK conflict).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=ErrorCode.PERSONA_SKILL_ACTIVATION_INVALID.value,
        ) from exc

    return await _persona_skills_payload(db, persona_id)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class ChannelUpdate(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=8192)
    # Explicit None clears the override (the resolver then falls back to
    # the globally-default persona). The repo layer uses a sentinel to
    # distinguish "omitted" from "set to NULL".
    default_persona_id: int | None = None


@router.get("/channels", response_model=ChannelPromptListResponse)
async def list_channels(request: Request) -> dict[str, Any]:
    rows = await channels_repo.list_all(_db(request))
    return {"channels": [_channel_to_dict(r) for r in rows]}


@router.put("/channels/{channel}", response_model=ChannelPromptResponse)
async def update_channel(
    channel: str, body: ChannelUpdate, request: Request
) -> dict[str, Any]:
    if channel not in CHANNEL_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.CHANNEL_NOT_FOUND.value,
                "params": {"channel": channel},
            },
        )
    db = _db(request)

    if body.default_persona_id is not None:
        target = await personas_repo.get(db, body.default_persona_id)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": ErrorCode.PERSONA_REF_INVALID.value,
                    "params": {"id": body.default_persona_id},
                },
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
            detail={
                "code": ErrorCode.CHANNEL_ROW_MISSING.value,
                "params": {"channel": channel},
            },
        )
    return _channel_to_dict(updated)


@router.post(
    "/channels/{channel}/reset", response_model=ChannelPromptResponse
)
async def reset_channel_prompt(
    channel: str, request: Request
) -> dict[str, Any]:
    if channel not in CHANNEL_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.CHANNEL_NOT_FOUND.value,
                "params": {"channel": channel},
            },
        )
    reset = await channels_repo.reset_prompt(_db(request), channel)
    if reset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.CHANNEL_ROW_MISSING.value,
                "params": {"channel": channel},
            },
        )
    return _channel_to_dict(reset)
