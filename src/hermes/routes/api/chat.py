"""`/chat`, `/chat/runs/{id}/cancel`, `/chat/context`, `/models` endpoints.

The actual streaming engine lives in `chat_stream.py`; this module owns the
request models, request parsing, conversation/attachment setup, and the
non-streaming helper endpoints. It re-exports the streaming engine plus the
error helpers so legacy imports of `_stream_web_agent_run` /
`_classify_chat_error` / `_sanitize_upstream_message` keep working."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.auth import current_user_id
from hermes.errors import ErrorCode
from hermes.events import (
    ChatStreamEnvelope,
)
from hermes.logging import logger
from hermes.personas import resolve_chat_context_meta
from hermes.provider_models import ProviderModelsError, list_provider_models
from hermes.repository import (
    attachments,
    conversations,
    messages,
)
from hermes.repository import (
    llm_credentials as llm_credentials_repo,
)
from hermes.thinking import resolve_thinking_support
from hermes.upstream import build_client_for_credential

from .chat_stream import (
    CLINE_CHANNEL,
    WEB_CHANNEL,
    _classify_chat_error,
    _sanitize_upstream_message,
    _stream_web_agent_run,
)

router = APIRouter()

# Re-exports for back-compat with tests and any external imports that pulled
# these names off `hermes.routes.api` directly.
__all__ = [
    "CLINE_CHANNEL",
    "ChatContextResponse",
    "ChatRequest",
    "ModelEntry",
    "ModelsResponse",
    "ThinkingSupportDTO",
    "WEB_CHANNEL",
    "_classify_chat_error",
    "_derive_conversation_title",
    "_sanitize_upstream_message",
    "_stream_web_agent_run",
    "build_client_for_credential",
    "list_provider_models",
    "resolve_chat_context_meta",
    "router",
]


def _derive_conversation_title(message: str, *, max_len: int = 60) -> str:
    title = " ".join(message.split())
    if not title:
        return "New chat"
    if len(title) <= max_len:
        return title
    return f"{title[: max_len - 3].rstrip()}..."


# ---------------------------------------------------------------------------
# /api/chat
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: int | None = None
    # Ids of attachments previously uploaded to this conversation (Plan 11).
    # Each must belong to `conversation_id` and still be unlinked, else 400.
    attachment_ids: list[int] = Field(default_factory=list)
    # One-turn overrides. Not persisted. Cleared after the agent run.
    model_override: str | None = Field(default=None, min_length=1)
    persona_id_override: int | None = Field(default=None, ge=1)
    thinking_budget: Literal["low", "medium", "high"] | None = None
    skill_hints: list[str] = Field(default_factory=list)


class ChatContextResponse(BaseModel):
    """Lightweight metadata about the currently-resolved persona + model.

    Used by the chat header pill to display the active agent identity.
    Does NOT include the system_prompt (large; computed per-turn only).
    """
    persona_id: int | None
    persona_name: str | None
    model: str


class ThinkingSupportDTO(BaseModel):
    supported: bool
    levels: list[str]


class ModelEntry(BaseModel):
    id: str
    credential_id: int
    credential_name: str
    provider: str
    thinking: ThinkingSupportDTO


class ModelsResponse(BaseModel):
    models: list[ModelEntry]


@router.post(
    "/chat",
    # The actual response is a StreamingResponse (text/event-stream), opaque to
    # OpenAPI. Declaring the envelope here is documentation-only: it registers
    # ChatStreamEnvelope (and every event subtype) as a schema component so the
    # generated TS types include the discriminated union the frontend parses.
    responses={
        200: {
            "model": ChatStreamEnvelope,
            "description": "SSE stream of chat events (one envelope per block).",
        }
    },
)
async def api_chat(request: Request) -> Response:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=ErrorCode.REQUEST_INVALID_JSON.value
        ) from exc

    try:
        payload = ChatRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.INVALID_REQUEST.value,
                "params": {"message": str(exc)},
            },
        ) from exc

    db: AsyncEngine = request.app.state.db

    # Attachments are uploaded to an existing conversation, so they can't be
    # paired with the implicit "create a new conversation" path — reject
    # before we'd create an empty thread that the 400 below would orphan.
    if payload.conversation_id is None and payload.attachment_ids:
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.ATTACHMENT_REQUIRES_CONVERSATION.value,
        )

    if payload.conversation_id is None:
        convo = await conversations.create(
            db,
            user_id=current_user_id(request),
            channel=WEB_CHANNEL,
            title=_derive_conversation_title(payload.message),
        )
    else:
        existing = await conversations.get(
            db, payload.conversation_id, user_id=current_user_id(request)
        )
        if existing is None:
            raise HTTPException(
                status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
            )
        # /api/chat is the interactive surface — never let it append into a
        # task thread, which would blur channel semantics and bypass the
        # per-channel system prompt + tool catalog.
        if existing.channel not in (WEB_CHANNEL, CLINE_CHANNEL):
            raise HTTPException(
                status_code=400,
                detail=ErrorCode.CONVERSATION_NOT_WEB.value,
            )
        convo = existing

    # Validate attachment ownership before persisting anything: every id
    # must reference an unlinked upload belonging to this conversation.
    uid = current_user_id(request)
    if payload.attachment_ids:
        for aid in payload.attachment_ids:
            att = await attachments.get(db, aid, user_id=uid)
            if (
                att is None
                or att.conversation_id != convo.id
                or att.message_id is not None
            ):
                raise HTTPException(
                    status_code=400,
                    detail=ErrorCode.ATTACHMENT_UNKNOWN_IDS.value,
                )

    user_msg = await messages.append(
        db,
        user_id=uid,
        conversation_id=convo.id,
        role="user",
        content=payload.message,
    )
    if payload.attachment_ids:
        await attachments.link_to_message(
            db,
            user_id=uid,
            attachment_ids=payload.attachment_ids,
            message_id=user_msg.id,
            conversation_id=convo.id,
        )

    return await _stream_web_agent_run(
        request,
        convo,
        model_override=payload.model_override,
        persona_id_override=payload.persona_id_override,
        thinking_budget=payload.thinking_budget,
        skill_hints=payload.skill_hints,
    )


@router.post(
    "/chat/runs/{run_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    # Declare 404 explicitly so the generated frontend types include it —
    # without this the OpenAPI doc only lists 204, and the client has to
    # special-case the "run already finished" race without type support.
    responses={404: {"description": "Unknown or already-finished run_id"}},
)
async def api_chat_cancel_run(request: Request, run_id: str) -> Response:
    """Cooperatively cancel an in-flight /api/chat run.

    Returns 204 once the cancellation signal is delivered. The actual
    `cancelled` SSE event is emitted on the streaming response when the
    agent reaches the next safe step. Unknown / already-finished run IDs
    return 404 — we don't pretend a cancel succeeded for a run we no
    longer track.
    """
    chat_runs: dict[str, asyncio.Event] = request.app.state.chat_runs
    event = chat_runs.get(run_id)
    if event is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.RUN_NOT_FOUND.value
        )
    event.set()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chat/context")
async def api_chat_context(request: Request) -> ChatContextResponse:
    """Return the currently-resolved persona name + model for the web channel.

    Resolves persona → credential → model using the same priority chain as
    POST /api/chat but skips the (expensive) system-prompt build. Suitable
    for polling from the header pill. Returns 503 if no credential is
    configured (same condition that would block a real chat turn).
    """
    db: AsyncEngine = request.app.state.db
    persona_id, persona_name, model = await resolve_chat_context_meta(
        WEB_CHANNEL, db, user_id=current_user_id(request)
    )
    return ChatContextResponse(
        persona_id=persona_id,
        persona_name=persona_name,
        model=model,
    )


@router.get("/models")
async def api_models(request: Request) -> ModelsResponse:
    """Return all models available across all configured credentials.

    Delegates the actual listing to `provider_models.list_provider_models`
    so we share its 10 min per-credential cache and pick up OpenRouter's
    `supported_parameters` metadata for capability resolution. Falls back
    to `cred.model` when the lister raises (network / non-listing
    provider). Each entry carries a `thinking` block telling the composer
    which budgets to offer.
    """
    db: AsyncEngine = request.app.state.db
    credentials = await llm_credentials_repo.list_all(db, user_id=current_user_id(request))
    encryptor = request.app.state.encryptor
    http: httpx.AsyncClient = request.app.state.external_http

    entries: list[ModelEntry] = []
    for cred in credentials:
        try:
            choices = await list_provider_models(
                cred, http=http, encryptor=encryptor
            )
        except ProviderModelsError as exc:
            logger.debug(
                "model listing failed for credential %s; falling back to "
                "configured model: %s",
                cred.id,
                exc,
            )
            choices = ()

        if choices:
            for choice in choices:
                support = resolve_thinking_support(
                    cred.provider,
                    choice.id,
                    list(choice.supported_parameters)
                    if choice.supported_parameters is not None
                    else None,
                )
                entries.append(ModelEntry(
                    id=choice.id,
                    credential_id=cred.id,
                    credential_name=cred.display_name,
                    provider=cred.provider,
                    thinking=ThinkingSupportDTO(
                        supported=support.supported,
                        levels=list(support.levels),
                    ),
                ))
        elif cred.model:
            support = resolve_thinking_support(cred.provider, cred.model, None)
            entries.append(ModelEntry(
                id=cred.model,
                credential_id=cred.id,
                credential_name=cred.display_name,
                provider=cred.provider,
                thinking=ThinkingSupportDTO(
                    supported=support.supported,
                    levels=list(support.levels),
                ),
            ))

    return ModelsResponse(models=entries)
