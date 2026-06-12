"""Conversation CRUD + attachment upload + retry / edit-and-regenerate.

Owns POST/GET/PATCH/DELETE on `/conversations` plus the nested
`/conversations/{conv_id}/*` endpoints (bookmark, retry, edit-and-regenerate,
attachments). The streaming re-run hands off to `chat_stream._stream_web_agent_run`."""

from __future__ import annotations

import contextlib
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import attachments as attachments_mod
from hermes.auth import current_user_id
from hermes.config import conversation_scratch_root
from hermes.errors import ErrorCode
from hermes.repository import (
    attachments,
    conversations,
    messages,
)
from hermes.routes._helpers import validate_limit

from .chat_stream import CLINE_CHANNEL, WEB_CHANNEL, _stream_web_agent_run

router = APIRouter()


def _derive_conversation_title(message: str, *, max_len: int = 60) -> str:
    # Duplicated from chat.py rather than imported to keep this submodule
    # self-contained — only used here on the explicit-create path.
    title = " ".join(message.split())
    if not title:
        return "New chat"
    if len(title) <= max_len:
        return title
    return f"{title[: max_len - 3].rstrip()}..."


# ---------------------------------------------------------------------------
# /api/conversations
# ---------------------------------------------------------------------------


class ConversationResponse(BaseModel):
    id: int
    channel: str
    title: str | None
    started_at: int
    updated_at: int
    bookmarked: bool
    # unix epoch seconds; null when the conversation is bookmarked
    # (never expires).
    expires_at: int | None


class ConversationSummaryResponse(ConversationResponse):
    message_count: int


def _conversation_to_dict(c: Any) -> dict[str, Any]:
    return {
        "id": c.id,
        "channel": c.channel,
        "title": c.title,
        "started_at": c.started_at,
        "updated_at": c.updated_at,
        "bookmarked": bool(c.bookmarked),
        "expires_at": c.expires_at,
    }


class ToolCallView(BaseModel):
    """Structured view of a completed tool call, reconstructed from a persisted
    `role:"tool"` message's `meta_json` + content so the frontend can render
    the same card on reload as it showed live. `result` is set on success,
    `error` on failure."""

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "error"]
    result: str | None = None
    error: str | None = None


class AttachmentResponse(BaseModel):
    id: int
    conversation_id: int
    # Null while staged (uploaded, not yet sent); set once the message it
    # belongs to has been sent.
    message_id: int | None = None
    filename: str
    content_type: str
    size: int
    created_at: int


class MessageResponse(BaseModel):
    id: int
    role: Literal["user", "assistant", "tool"]
    content: str
    ts: int
    # Populated only for tool turns; null for user/assistant messages.
    tool_call: ToolCallView | None = None
    # The model's reasoning for an assistant turn, when the provider exposed
    # any (persisted in meta_json by run_agent). Null otherwise — including for
    # every user/tool turn — so the reasoning card only renders where relevant.
    reasoning: str | None = None
    # Files attached to a user turn (Plan 11). Empty for assistant/tool turns
    # and for user turns sent without attachments.
    attachments: list[AttachmentResponse] = Field(default_factory=list)


def _attachment_to_dict(a: Any) -> dict[str, Any]:
    return {
        "id": a.id,
        "conversation_id": a.conversation_id,
        "message_id": a.message_id,
        "filename": a.filename,
        "content_type": a.content_type,
        "size": a.size,
        "created_at": a.created_at,
    }


async def _unlink_attachment_files_after(
    db: AsyncEngine, conv_id: int, *, user_id: int, after_id: int
) -> None:
    """Delete the on-disk blobs of attachments linked to messages after
    `after_id`. Their DB rows are removed by the messages CASCADE when the
    caller trims those turns; this reclaims the files so they don't leak in
    the scratch dir until the whole conversation is deleted."""
    leaked = await attachments.list_after_message(
        db, user_id=user_id, conversation_id=conv_id, after_message_id=after_id
    )
    for att in leaked:
        with contextlib.suppress(OSError):
            attachments_mod.file_path(att).unlink(missing_ok=True)


def _tool_call_view_from_message(m: Any) -> ToolCallView | None:
    """Build a ToolCallView from a persisted tool message. Tolerates the
    pre-Plan-08 meta_json shape (`{tool_call_id, name}` with no arguments /
    status) by defaulting status to "success" and arguments to {}."""
    if m.role != "tool":
        return None
    meta: dict[str, Any] = {}
    if m.meta_json:
        try:
            decoded = json.loads(m.meta_json)
            meta = decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            meta = {}
    status = meta.get("status", "success")
    args = meta.get("arguments")
    return ToolCallView(
        call_id=meta.get("tool_call_id", ""),
        name=meta.get("name", ""),
        arguments=args if isinstance(args, dict) else {},
        status=status if status in ("success", "error") else "success",
        result=m.content if status != "error" else None,
        error=m.content if status == "error" else None,
    )


def _reasoning_from_message(m: Any) -> str | None:
    """Pull persisted reasoning off an assistant turn's meta_json (the
    `reasoning` key run_agent writes). Null for every other role / shape so
    the field stays absent wherever there's nothing to show."""
    if m.role != "assistant" or not m.meta_json:
        return None
    try:
        meta = json.loads(m.meta_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    reasoning = meta.get("reasoning")
    return reasoning if isinstance(reasoning, str) and reasoning else None


def _message_to_dict(
    m: Any, atts: list[Any] | None = None
) -> dict[str, Any]:
    view = _tool_call_view_from_message(m)
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "ts": m.ts,
        "tool_call": view.model_dump() if view is not None else None,
        "reasoning": _reasoning_from_message(m),
        "attachments": [_attachment_to_dict(a) for a in (atts or [])],
    }


class ConversationDetailResponse(BaseModel):
    conversation: ConversationResponse
    messages: list[MessageResponse]


class ConversationUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ConversationCreateRequest(BaseModel):
    # Optional seed text (typically the first message) used to derive a
    # title, mirroring what /api/chat does when it auto-creates. The
    # conversation itself is created empty — the message is sent separately.
    message: str | None = None


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def api_create_conversation(
    request: Request, body: ConversationCreateRequest
) -> dict[str, Any]:
    """Create an empty web conversation. The web UI needs this to attach
    files to the very first message: uploads are tied to a conversation id
    at upload time (Plan 11), so the conversation must exist before the
    first send."""
    db: AsyncEngine = request.app.state.db
    title = (
        _derive_conversation_title(body.message)
        if body.message and body.message.strip()
        else None
    )
    convo = await conversations.create(
        db, user_id=current_user_id(request), channel=WEB_CHANNEL, title=title
    )
    return _conversation_to_dict(convo)


@router.get("/conversations", response_model=list[ConversationSummaryResponse])
async def api_list_conversations(
    request: Request,
    channel: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    user_id = current_user_id(request)
    if q is not None and q.strip():
        convos = await conversations.search(
            db, user_id=user_id, query=q, channel=channel, limit=limit
        )
    else:
        convos = await conversations.list_all(
            db, user_id=user_id, channel=channel, limit=limit
        )
    out: list[dict[str, Any]] = []
    for c in convos:
        count = await conversations.message_count(db, c.id, user_id=user_id)
        item = _conversation_to_dict(c)
        item["message_count"] = count
        out.append(item)
    return out


@router.get("/conversations/{conv_id}", response_model=ConversationDetailResponse)
async def api_get_conversation(
    request: Request, conv_id: int, limit: int = 200
) -> dict[str, Any]:
    limit = validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    uid = current_user_id(request)
    convo = await conversations.get(db, conv_id, user_id=uid)
    if convo is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    msgs = await messages.list_by_conversation(db, conv_id, user_id=uid, limit=limit)
    atts_by_message: dict[int, list[Any]] = {}
    for att in await attachments.list_by_conversation(db, conv_id, user_id=uid):
        if att.message_id is not None:
            atts_by_message.setdefault(att.message_id, []).append(att)
    return {
        "conversation": _conversation_to_dict(convo),
        "messages": [
            _message_to_dict(m, atts_by_message.get(m.id)) for m in msgs
        ],
    }


@router.patch("/conversations/{conv_id}", response_model=ConversationResponse)
async def api_update_conversation(
    request: Request, conv_id: int, body: ConversationUpdateRequest
) -> dict[str, Any]:
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(
            status_code=400, detail=ErrorCode.CONVERSATION_TITLE_BLANK.value
        )

    db: AsyncEngine = request.app.state.db
    updated = await conversations.update_title(
        db, conv_id, user_id=current_user_id(request), title=title
    )
    if updated is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    return _conversation_to_dict(updated)


@router.delete("/conversations/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_conversation(request: Request, conv_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    deleted = await conversations.delete(
        db,
        conv_id,
        user_id=current_user_id(request),
        scratch_root=conversation_scratch_root(),
    )
    if not deleted:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/conversations/{conv_id}/bookmark", response_model=ConversationResponse
)
async def api_toggle_bookmark_conversation(
    request: Request, conv_id: int
) -> dict[str, Any]:
    """Toggle the conversation's bookmarked flag. Bookmarked rows have
    `expires_at = NULL` and survive the daily sweep; unbookmarking
    re-arms the TTL from now."""
    db: AsyncEngine = request.app.state.db
    user_id = current_user_id(request)
    existing = await conversations.get(db, conv_id, user_id=user_id)
    if existing is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    updated = await conversations.set_bookmarked(
        db, conv_id, user_id=user_id, bookmarked=not existing.bookmarked
    )
    if updated is None:
        # Lost-the-race between get() and set_bookmarked().
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    return _conversation_to_dict(updated)


@router.post("/conversations/{conv_id}/retry")
async def api_retry_conversation(request: Request, conv_id: int) -> Response:
    """Regenerate the latest assistant response.

    Trims every assistant/tool turn that followed the last user message,
    then re-runs the web agent over the surviving context and streams the
    new reply with the same SSE semantics as /api/chat.
    """
    db: AsyncEngine = request.app.state.db

    uid = current_user_id(request)
    convo = await conversations.get(db, conv_id, user_id=uid)
    if convo is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    # Same channel guard as /api/chat: retry is an interactive surface.
    if convo.channel not in (WEB_CHANNEL, CLINE_CHANNEL):
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.CONVERSATION_NOT_WEB.value,
        )

    last_user = await messages.last_user_message(db, conv_id, user_id=uid)
    if last_user is None:
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.CONVERSATION_NO_USER_MESSAGE_TO_RETRY.value,
        )

    # Drop the assistant/tool tail so run_agent regenerates from the same
    # context that produced the original reply (simplest persistence
    # strategy — no superseded_at bookkeeping).
    await messages.delete_after(db, conv_id, user_id=uid, after_id=last_user.id)

    return await _stream_web_agent_run(request, convo)


class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1)


@router.post("/conversations/{conv_id}/messages/{message_id}/edit-and-regenerate")
async def api_edit_and_regenerate(
    request: Request, conv_id: int, message_id: int, body: EditMessageRequest
) -> Response:
    """Edit a user message and regenerate the conversation from that point.

    Replaces the message's content in place, trims every later turn (the same
    delete-then-rerun mechanic as /retry, keyed on the edited message id rather
    than the last user message), then re-runs the web agent over the surviving
    context and streams with the same SSE semantics as /api/chat.

    Unlike /api/chat, the body is a declared model so the request schema lands
    in the OpenAPI doc (and the generated frontend types). Empty/missing
    content therefore fails FastAPI validation with a 422.
    """
    db: AsyncEngine = request.app.state.db

    uid = current_user_id(request)
    convo = await conversations.get(db, conv_id, user_id=uid)
    if convo is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    # Same channel guard as /api/chat: edit is an interactive surface.
    if convo.channel not in (WEB_CHANNEL, CLINE_CHANNEL):
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.CONVERSATION_NOT_WEB.value,
        )

    target = await messages.get(db, message_id, user_id=uid)
    # A message outside this conversation is a 404 (not found *here*), so the
    # path's conv_id is authoritative and clients can't edit across threads.
    if target is None or target.conversation_id != conv_id:
        raise HTTPException(
            status_code=404, detail=ErrorCode.MESSAGE_NOT_FOUND.value
        )
    if target.role != "user":
        raise HTTPException(
            status_code=400, detail=ErrorCode.MESSAGE_ONLY_USER_EDITABLE.value
        )

    updated = await messages.update_content(
        db, message_id, user_id=uid, content=body.content
    )
    if updated is None:
        # Lost-the-race between the get() above and the update — the message
        # was deleted concurrently. Don't trim/regenerate on a phantom edit.
        raise HTTPException(
            status_code=404, detail=ErrorCode.MESSAGE_NOT_FOUND.value
        )
    # Drop everything after the edited turn so run_agent regenerates from the
    # corrected context (simplest persistence strategy — no superseded_at).
    # Unlink the on-disk files of any attachments on those later turns first:
    # delete_after's CASCADE reclaims the rows but not the scratch-dir blobs.
    await _unlink_attachment_files_after(db, conv_id, user_id=uid, after_id=message_id)
    await messages.delete_after(db, conv_id, user_id=uid, after_id=message_id)

    return await _stream_web_agent_run(request, convo)
