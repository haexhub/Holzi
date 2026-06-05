"""HTTP API for personas + channel prompts (Plan 29-A → 36 → 37).

Two thin CRUD surfaces over the `personas` / `channel_prompts` tables.
The per-persona skill-activation layer (Plan 33) was dropped in Plan 37
in favour of the global catalog-index + `skill_load` tool pattern.
The single-default invariant on personas is held by triggers in
`schema.sql`; this layer is responsible for the API-level guardrails
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
"""
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.errors import ErrorCode
from hermes.personas import CHANNEL_REGISTRY
from hermes.provider_models import ProviderModelsError, list_provider_models
from hermes.repository import channels as channels_repo
from hermes.repository import llm_credentials as llm_credentials_repo
from hermes.repository import persona_history as persona_history_repo
from hermes.repository import personas as personas_repo
from hermes.repository.models import (
    ChannelPromptRow,
    Persona,
    PersonaHistory,
)
from hermes.repository.personas import _UNSET as REPO_UNSET

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Response models — kept Pydantic so OpenAPI emits proper TS types for
# `pnpm run gen:api`. The repo layer returns dataclasses; conversion is
# trivial and lives in `_persona_to_dict` / `_channel_to_dict` below.
# ---------------------------------------------------------------------------


class PersonaResponse(BaseModel):
    id: int
    name: str
    soul: str
    identity: str
    agents: str
    is_default: bool
    created_at: int
    updated_at: int
    llm_credential_id: int | None = None
    model: str | None = None


class PersonaListResponse(BaseModel):
    personas: list[PersonaResponse]


class PersonaHistorySnapshot(BaseModel):
    """The parsed `persona_history.snapshot_json` body — exactly the four
    fields written by `personas_repo.create`/`update` (and the lifespan
    migration). `is_default` is deliberately excluded; it's a sort flag
    on the live `personas` row, not a persona-version property.

    Typed as a named model so `gen:api` emits a TypeScript interface
    with named fields instead of an opaque index signature — a typo in
    `entry.snapshot.<field>` on the FE is then caught by tsc.
    """

    name: str
    soul: str
    identity: str
    agents: str


class PersonaHistoryItem(BaseModel):
    id: int
    persona_id: int
    author: str
    snapshot: PersonaHistorySnapshot
    created_at: int


class PersonaHistoryListResponse(BaseModel):
    history: list[PersonaHistoryItem]


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
        "soul": p.soul,
        "identity": p.identity,
        "agents": p.agents,
        "is_default": p.is_default,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "llm_credential_id": p.llm_credential_id,
        "model": p.model,
    }


def _history_to_dict(h: PersonaHistory) -> dict[str, Any]:
    return {
        "id": h.id,
        "persona_id": h.persona_id,
        "author": h.author,
        "snapshot": json.loads(h.snapshot_json),
        "created_at": h.created_at,
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
    # `extra="forbid"` rejects legacy `prompt`-keyed bodies with a Pydantic
    # 422 — the wire contract is now three fragments, no transition shim.
    model_config = ConfigDict(extra="forbid")
    # 1..64 chars matches the "Hermes der Direkte"-class names the user will
    # pick; longer values are almost certainly accidental paste.
    name: str = Field(min_length=1, max_length=64)
    # 8192 per fragment is a hard upper bound; the LLM doesn't need more
    # identity text and anything bigger is probably the user dumping a whole
    # document. Default "" means "section omitted" — the resolver drops
    # empty sections from the composed prompt. At least one fragment must
    # be non-empty; that's a route-level check (see `create_persona`) so
    # the 422 detail shape stays `{code, params}`.
    soul: str = Field(default="", max_length=8192)
    identity: str = Field(default="", max_length=8192)
    agents: str = Field(default="", max_length=8192)
    is_default: bool = False


class PersonaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=64)
    soul: str | None = Field(default=None, max_length=8192)
    identity: str | None = Field(default=None, max_length=8192)
    agents: str | None = Field(default=None, max_length=8192)
    is_default: bool | None = None
    llm_credential_id: int | None = None
    model: str | None = None


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
    # All-empty bodies aren't useful — without any fragment the persona
    # contributes nothing to the resolver output. Enforced here (not via
    # a `model_validator`) so the 422 detail shape stays `{code, params}`
    # consistent with the rest of the API.
    if not (
        body.soul.strip() or body.identity.strip() or body.agents.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": ErrorCode.PERSONA_FRAGMENTS_ALL_EMPTY.value,
                "params": {},
            },
        )
    try:
        persona = await personas_repo.create(
            _db(request),
            name=body.name,
            soul=body.soul,
            identity=body.identity,
            agents=body.agents,
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

    # All-empty check on the POST-merge state: fields with `None` keep
    # their existing value, otherwise the patch wins. If the result would
    # have all three fragments empty (after `.strip()`), refuse — the
    # resolver would compose an empty persona section, which is useless.
    merged_soul = existing.soul if body.soul is None else body.soul
    merged_identity = (
        existing.identity if body.identity is None else body.identity
    )
    merged_agents = existing.agents if body.agents is None else body.agents
    if not (
        merged_soul.strip()
        or merged_identity.strip()
        or merged_agents.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": ErrorCode.PERSONA_FRAGMENTS_ALL_EMPTY.value,
                "params": {},
            },
        )

    # Resolve new credential/model fields — only touch if they appear in the request body.
    new_cred_id = REPO_UNSET
    new_model = REPO_UNSET

    if "llm_credential_id" in body.model_fields_set:
        if body.llm_credential_id is not None:
            cred = await llm_credentials_repo.get(db, body.llm_credential_id)
            if cred is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "code": ErrorCode.PERSONA_INVALID_CREDENTIAL.value,
                        "params": {"id": body.llm_credential_id},
                    },
                )
        new_cred_id = body.llm_credential_id

    if "model" in body.model_fields_set:
        if body.model is not None:
            # model can only be set when a credential is configured (new or existing)
            effective_cred_id = (
                body.llm_credential_id
                if "llm_credential_id" in body.model_fields_set
                else existing.llm_credential_id
            )
            if effective_cred_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "code": ErrorCode.PERSONA_INVALID_MODEL.value,
                        "params": {"model": body.model, "reason": "no_credential"},
                    },
                )
            cred_for_model = await llm_credentials_repo.get(db, effective_cred_id)
            if cred_for_model is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "code": ErrorCode.PERSONA_INVALID_CREDENTIAL.value,
                        "params": {"id": effective_cred_id},
                    },
                )
            try:
                available_models = await list_provider_models(
                    cred_for_model,
                    http=request.app.state.external_http,
                    encryptor=request.app.state.encryptor,
                )
            except ProviderModelsError:
                available_models = ()
            if not any(m.id == body.model for m in available_models):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "code": ErrorCode.PERSONA_INVALID_MODEL.value,
                        "params": {"model": body.model},
                    },
                )
        new_model = body.model

    try:
        updated = await personas_repo.update(
            db,
            persona_id,
            name=body.name,
            soul=body.soul,
            identity=body.identity,
            agents=body.agents,
            is_default=body.is_default,
            llm_credential_id=new_cred_id,
            model=new_model,
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


@router.get("/personas/{persona_id}/models")
async def list_persona_models(persona_id: int, request: Request) -> dict[str, Any]:
    """Return the model list for this persona's credential (or the active credential).

    Wraps GET /api/llm/credentials/{id}/models so the UI doesn't need to know
    which credential a persona uses.
    """
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

    cred = None
    if persona.llm_credential_id is not None:
        cred = await llm_credentials_repo.get(db, persona.llm_credential_id)
    if cred is None:
        cred = await llm_credentials_repo.get_active(db)
    if cred is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorCode.PERSONA_NO_CREDENTIAL.value,
        )

    try:
        models = await list_provider_models(
            cred,
            http=request.app.state.external_http,
            encryptor=request.app.state.encryptor,
        )
    except ProviderModelsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"models": [{"id": m.id, "label": m.label} for m in models]}


# ---------------------------------------------------------------------------
# Persona history (Plan 36)
# ---------------------------------------------------------------------------


@router.get(
    "/personas/{persona_id}/history",
    response_model=PersonaHistoryListResponse,
)
async def list_persona_history(
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
    rows = await persona_history_repo.list_for_persona(db, persona_id)
    return {"history": [_history_to_dict(r) for r in rows]}


@router.post(
    "/personas/{persona_id}/history/{snapshot_id}/restore",
    response_model=PersonaResponse,
)
async def restore_persona_history(
    persona_id: int, snapshot_id: int, request: Request
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
    snapshot = await persona_history_repo.get(db, snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.PERSONA_HISTORY_NOT_FOUND.value,
                "params": {"id": snapshot_id},
            },
        )
    # Guard against cross-persona restore — the snapshot belongs to a
    # different persona, so applying it would silently rename + overwrite
    # the target. Refuse with a 422 the FE can surface as a routing error.
    if snapshot.persona_id != persona_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": ErrorCode.PERSONA_HISTORY_PERSONA_MISMATCH.value,
                "params": {
                    "persona_id": persona_id,
                    "snapshot_id": snapshot_id,
                },
            },
        )
    snap = json.loads(snapshot.snapshot_json)
    # Restore = update the persona with the snapshot's fields. The repo
    # `update()` auto-writes a NEW history row inside the same txn (the
    # audit trail of the restore action itself), so the Verlauf-Tab will
    # show snapshot-1, snapshot-2 (later state), snapshot-3 (restore-of-1).
    try:
        updated = await personas_repo.update(
            db,
            persona_id,
            name=snap["name"],
            soul=snap["soul"],
            identity=snap["identity"],
            agents=snap["agents"],
        )
    except IntegrityError as exc:
        # The snapshot's `name` could now collide with a sibling persona
        # that was renamed after the snapshot was taken. Surface as the
        # same 409 PERSONA_NAME_CONFLICT shape as POST/PUT.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": ErrorCode.PERSONA_NAME_CONFLICT.value,
                "params": {"name": snap["name"]},
            },
        ) from exc
    if updated is None:
        # Race: persona was deleted between the get and the update.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": ErrorCode.PERSONA_NOT_FOUND.value,
                "params": {"id": persona_id},
            },
        )
    return _persona_to_dict(updated)


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
