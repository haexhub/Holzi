"""Shared Pydantic request/response models + dict-conversion helpers
for the `preferences` package.

Kept private to the package — external callers should use the HTTP
surface (and the FastAPI-generated OpenAPI schema), not these classes
directly. Split out of the original single-file `preferences.py` to
satisfy the >500-LoC rule; the wire contract is unchanged."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import CHANNEL_REGISTRY
from hermes.repository.models import (
    ChannelPromptRow,
    Persona,
    PersonaHistory,
)

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


# ---------------------------------------------------------------------------
# Personas — request bodies
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


# ---------------------------------------------------------------------------
# Channels — request bodies
# ---------------------------------------------------------------------------


class ChannelUpdate(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=8192)
    # Explicit None clears the override (the resolver then falls back to
    # the globally-default persona). The repo layer uses a sentinel to
    # distinguish "omitted" from "set to NULL".
    default_persona_id: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
