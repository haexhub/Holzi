"""The `/api/*` HTTP surface.

This package was split out of a single 1.8k-LoC `api.py` for the >500-LoC
rule; the public surface is unchanged — `main.py` still imports `router`
from `hermes.routes.api`.

Sub-routers are bare `APIRouter()` instances; this aggregator owns the
`/api` prefix so each route's path stays a literal match for the docstring
above its handler. Tests that reach into module-level symbols (e.g.
`_classify_chat_error`, `_sanitize_upstream_message`) keep working via the
re-exports below."""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    approvals,
    attachments,
    chat,
    conversations,
    notes,
    runs,
    sandbox,
    tasks,
)
from .chat import (
    ChatContextResponse,
    ChatRequest,
    ModelEntry,
    ModelsResponse,
    ThinkingSupportDTO,
    _derive_conversation_title,
    build_client_for_credential,
    list_provider_models,
    resolve_chat_context_meta,
)
from .chat_stream import (
    CLINE_CHANNEL,
    WEB_CHANNEL,
    _classify_chat_error,
    _sanitize_upstream_message,
    _stream_web_agent_run,
    resolve_persona_context,
)

router = APIRouter(prefix="/api")
router.include_router(chat.router)
router.include_router(approvals.router)
router.include_router(runs.router)
router.include_router(conversations.router)
router.include_router(attachments.router)
router.include_router(notes.router)
router.include_router(tasks.router)
router.include_router(sandbox.router)

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
    "resolve_persona_context",
    "router",
]
