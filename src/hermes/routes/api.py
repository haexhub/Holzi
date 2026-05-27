import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import ApprovalDecision, ChatRunCancelled, run_agent
from hermes.config import conversation_scratch_root, settings
from hermes.events import (
    ApprovalRequiredData,
    ApprovalRequiredEvent,
    CancelledEvent,
    ChatStreamEnvelope,
    DoneEvent,
    ErrorData,
    ErrorEvent,
    RunData,
    RunEvent,
    SessionData,
    SessionEvent,
    TextData,
    TextEvent,
    ToolCallData,
    ToolCallEvent,
    ToolResultData,
    ToolResultEvent,
    to_sse,
)
from hermes.logging import logger
from hermes.repository import (
    conversations,
    messages,
    notes,
    reminders,
    runs,
    todos,
)
from hermes.repository import (
    llm_credentials as llm_credentials_repo,
)
from hermes.run_tracker import track_run
from hermes.tool_catalog import build_tool_catalog

router = APIRouter(prefix="/api")

WEB_SYSTEM_PROMPT = (
    "You are Hermes, a personal AI assistant for Martin, talking through the "
    "web UI. Be concise and direct."
)

WEB_CHANNEL = "web"

# Approvals can take minutes; idle proxies (Traefik, mobile carriers) close
# silent SSE connections. Emit a comment heartbeat at this cadence whenever no
# real event is flowing so the connection stays warm while we wait.
SSE_HEARTBEAT_SECONDS = 15.0

# Negative LIMIT disables LIMIT in SQLite — refuse non-positive values at the
# API boundary so an authenticated client can't trigger an unbounded scan.
MAX_LIST_LIMIT = 500


def _validate_limit(limit: int, *, max_limit: int = MAX_LIST_LIMIT) -> int:
    if limit < 1 or limit > max_limit:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {max_limit}",
        )
    return limit


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


def _classify_chat_error(exc: BaseException) -> tuple[str, int, str]:
    """Map an exception raised inside the agent loop to an error code and the
    HTTP status it would correspond to in a non-streaming world.

    The /api/chat response is already 200 by the time we see the error
    (StreamingResponse has flushed headers), so the status code is reported
    to the client *inside* the SSE error event — the frontend uses it to
    distinguish "upstream provider is down" (502) from "upstream too slow"
    (504) from "our agent blew up" (500). Same triage logic mirrors what
    `routes/llm.py` does for `GET /models`.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        upstream_status = exc.response.status_code
        return (
            "upstream_http_error",
            502,
            f"upstream returned {upstream_status}",
        )
    if isinstance(exc, httpx.TimeoutException):
        return ("upstream_timeout", 504, "upstream timed out")
    if isinstance(exc, httpx.RequestError):
        return (
            "upstream_unreachable",
            502,
            f"could not reach upstream: {exc}",
        )
    return ("agent_error", 500, str(exc))


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
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    try:
        payload = ChatRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db: AsyncEngine = request.app.state.db

    if payload.conversation_id is None:
        convo = await conversations.create(
            db,
            channel=WEB_CHANNEL,
            title=_derive_conversation_title(payload.message),
        )
    else:
        existing = await conversations.get(db, payload.conversation_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown conversation_id")
        # /api/chat is the web surface — never let it append into a Signal or
        # VSCode thread, which would blur channel semantics and bypass the
        # per-channel system prompt + tool catalog.
        if existing.channel != WEB_CHANNEL:
            raise HTTPException(
                status_code=400,
                detail="conversation_id must reference a web conversation",
            )
        convo = existing

    await messages.append(
        db, conversation_id=convo.id, role="user", content=payload.message
    )

    return await _stream_web_agent_run(request, convo)


async def _stream_web_agent_run(request: Request, convo: Any) -> Response:
    """Run the web agent over the conversation's current message history and
    stream it as SSE. Shared by /api/chat (after appending the new user
    message) and /api/conversations/{id}/retry (after trimming the trailing
    assistant/tool tail) so retry is not a separate code path."""
    db: AsyncEngine = request.app.state.db
    upstream: httpx.AsyncClient = request.app.state.upstream

    tools = build_tool_catalog(
        db=db,
        signal_client=request.app.state.signal_client,
        signal_self_number=request.app.state.signal_self_number,
        external_http=request.app.state.external_http,
        brave_api_key=request.app.state.brave_api_key,
        current_channel=WEB_CHANNEL,
    )

    # Per-request cancellation handle. The registry on app.state maps
    # run_id → asyncio.Event; POST /api/chat/runs/{id}/cancel sets the
    # event, run_agent observes it between safe steps. Single-worker /
    # single-user invariant documented in hermes/agent.py.
    run_id = uuid.uuid4().hex
    chat_runs: dict[str, asyncio.Event] = request.app.state.chat_runs
    cancel_event = asyncio.Event()
    chat_runs[run_id] = cancel_event

    # approval_id → Future the agent task awaits while a risky tool is paused.
    # POST /api/approvals/{id} resolves it. Single-worker invariant (same as
    # chat_runs) makes this in-process registry correct.
    approvals: dict[str, asyncio.Future[ApprovalDecision]] = (
        request.app.state.approvals
    )

    # Active credential overrides settings.model; resolve once before the
    # SSE generator so the model id we persist in agent_runs matches what
    # the upstream actually saw.
    model = (
        await llm_credentials_repo.get_active_model(db)
    ) or settings.model

    async def gen() -> AsyncIterator[bytes]:
        yield to_sse(SessionEvent(data=SessionData(conversation_id=convo.id)))
        yield to_sse(RunEvent(data=RunData(run_id=run_id)))

        # Bridge run_agent's callbacks (called from inside the agent task) to
        # the SSE generator via an async queue carrying envelope events. `None`
        # is the sentinel that means "agent finished — drain and emit
        # done/error".
        queue: asyncio.Queue[BaseModel | None] = asyncio.Queue()
        # Mutated by run_agent when the upstream provides usage stats —
        # surfaced into the agent_runs row by track_run's finalize step.
        metrics: dict[str, Any] = {}

        async def on_chunk(chunk: str) -> None:
            await queue.put(TextEvent(data=TextData(content=chunk)))

        async def on_tool_call(call_id: str, name: str, args: dict[str, Any]) -> None:
            await queue.put(
                ToolCallEvent(
                    data=ToolCallData(call_id=call_id, name=name, arguments=args)
                )
            )

        async def on_tool_result(call_id: str, status: str, content: str) -> None:
            # status is the literal "success" | "error" run_agent reports.
            data = ToolResultData(
                call_id=call_id,
                status=status,  # type: ignore[arg-type]
                result=content if status == "success" else None,
                error=content if status == "error" else None,
            )
            await queue.put(ToolResultEvent(data=data))

        async def on_approval(
            call_id: str, name: str, args: dict[str, Any], reason: str
        ) -> ApprovalDecision:
            # Register a future, surface the card, then block the agent task
            # until POST /api/approvals/{id} resolves it. The SSE generator
            # keeps the connection alive with heartbeats meanwhile.
            approval_id = uuid.uuid4().hex
            future: asyncio.Future[ApprovalDecision] = (
                asyncio.get_running_loop().create_future()
            )
            approvals[approval_id] = future
            await queue.put(
                ApprovalRequiredEvent(
                    data=ApprovalRequiredData(
                        approval_id=approval_id,
                        call_id=call_id,
                        name=name,
                        arguments=args,
                        reason=reason,
                    )
                )
            )
            try:
                return await future
            finally:
                # Drop the entry however we leave (resolved, run cancelled,
                # client disconnect) so a later decision can't hit a stale id.
                approvals.pop(approval_id, None)

        async def run_task() -> None:
            try:
                async with track_run(
                    db,
                    run_id=run_id,
                    conversation_id=convo.id,
                    channel=WEB_CHANNEL,
                    model=model,
                    metrics=metrics,
                ):
                    await run_agent(
                        upstream=upstream,
                        db=db,
                        conversation_id=convo.id,
                        system_prompt=WEB_SYSTEM_PROMPT,
                        model=model,
                        tools=tools,
                        on_chunk=on_chunk,
                        on_tool_call=on_tool_call,
                        on_tool_result=on_tool_result,
                        on_approval=on_approval,
                        cancel_event=cancel_event,
                        metrics=metrics,
                    )
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_task())
        # Persist the pending queue.get() across heartbeat timeouts instead of
        # re-issuing it: `asyncio.wait_for(queue.get(), ...)` would cancel the
        # getter on timeout and can drop an item that arrived concurrently.
        # `asyncio.wait` leaves the getter intact, so no event is lost.
        get_item: asyncio.Task[BaseModel | None] | None = None
        try:
            while True:
                if get_item is None:
                    get_item = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait(
                    {get_item}, timeout=SSE_HEARTBEAT_SECONDS
                )
                if not done:
                    # No event for a while (typically: an approval is pending).
                    # Emit an SSE comment so proxies keep the connection open.
                    # Comment lines carry no event/data, so every client (and
                    # our own parser) ignores them.
                    yield b": ping\n\n"
                    continue
                item = get_item.result()
                get_item = None
                if item is None:
                    break
                yield to_sse(item)
            # Re-raise any exception the agent task swallowed via its finally.
            await task
            await conversations.touch(db, convo.id)
            yield to_sse(DoneEvent())
        except ChatRunCancelled:
            # User-initiated cancel. `cancelled` is the single terminal
            # event for this turn — no trailing `done`. The frontend
            # renders the turn as aborted instead of appending a fake
            # assistant message.
            yield to_sse(CancelledEvent())
        except Exception as exc:  # noqa: BLE001 — surface to client, don't crash worker
            code, status_code, message = _classify_chat_error(exc)
            logger.warning(
                "api_chat_agent_error",
                error=str(exc),
                code=code,
                status_code=status_code,
            )
            yield to_sse(
                ErrorEvent(
                    data=ErrorData(code=code, status_code=status_code, message=message)
                )
            )
        finally:
            # `finally` runs both on the error path AND on client disconnect.
            # Disconnect raises asyncio.CancelledError (BaseException, not
            # Exception) which would otherwise leak the background task and
            # let it keep firing tools / writing to the DB after the client
            # is gone. Cancel + drain — suppressing CancelledError because
            # we already know it's done.
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Drop the dangling getter on disconnect/error so it doesn't leak.
            if get_item is not None and not get_item.done():
                get_item.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await get_item
            # Unregister regardless of how we exited (done / error /
            # cancelled / client-disconnect). Leaving stale entries would
            # let a future cancel hit the wrong run after run_id reuse.
            chat_runs.pop(run_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


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
        raise HTTPException(status_code=404, detail="unknown run_id")
    event.set()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# POST /api/approvals/{approval_id} — resolve a paused, approval-gated tool.
# ---------------------------------------------------------------------------


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["allow_once", "deny"]
    # Optional note shown to the LLM on deny so it can adapt its next turn.
    reason: str | None = None


@router.post(
    "/approvals/{approval_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Unknown approval_id"},
        409: {"description": "Approval already resolved"},
    },
)
async def api_resolve_approval(
    request: Request, approval_id: str, body: ApprovalDecisionRequest
) -> Response:
    """Deliver the user's decision for a paused tool call.

    Resolves the `asyncio.Future` the agent task is awaiting; the agent then
    either runs the tool (`allow_once`) or feeds a denied result back to the
    LLM (`deny`). Unknown ids → 404; an id whose decision already landed →
    409 (the UI disables the buttons after one click, so this only fires on a
    genuine double-submit / stale tab). Single-worker invariant makes the
    in-process registry safe.
    """
    approvals: dict[str, asyncio.Future[ApprovalDecision]] = (
        request.app.state.approvals
    )
    future = approvals.get(approval_id)
    if future is None:
        raise HTTPException(status_code=404, detail="unknown approval_id")
    if future.done():
        raise HTTPException(status_code=409, detail="approval already resolved")
    future.set_result(
        ApprovalDecision(decision=body.decision, reason=body.reason)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# /api/runs — persistent agent_runs history for diagnostics.
# ---------------------------------------------------------------------------

# Mirrors the enum in repository/runs.py — keep them in sync.
RunStatus = Literal["running", "success", "cancelled", "error"]


class AgentRunResponse(BaseModel):
    id: str
    conversation_id: int
    channel: str
    model: str
    started_at: int
    finished_at: int | None
    status: RunStatus
    error_code: str | None
    error_message: str | None
    error_trace: str | None
    input_tokens: int | None
    output_tokens: int | None


def _agent_run_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "conversation_id": r.conversation_id,
        "channel": r.channel,
        "model": r.model,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "status": r.status,
        "error_code": r.error_code,
        "error_message": r.error_message,
        "error_trace": r.error_trace,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
    }


@router.get("/runs", response_model=list[AgentRunResponse])
async def api_list_runs(
    request: Request,
    conversation_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Newest-first listing of agent_runs rows for diagnostics.

    `status` accepts the same enum the table itself uses; an unknown
    value returns 400 rather than silently widening to "all rows".
    """
    limit = _validate_limit(limit)
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    if status is not None and status not in runs.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                "status must be one of "
                + ", ".join(sorted(runs.VALID_STATUSES))
            ),
        )
    db: AsyncEngine = request.app.state.db
    rows = await runs.list_runs(
        db,
        conversation_id=conversation_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [_agent_run_to_dict(r) for r in rows]


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


class MessageResponse(BaseModel):
    id: int
    role: Literal["user", "assistant", "tool"]
    content: str
    ts: int
    # Populated only for tool turns; null for user/assistant messages.
    tool_call: ToolCallView | None = None


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


def _message_to_dict(m: Any) -> dict[str, Any]:
    view = _tool_call_view_from_message(m)
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "ts": m.ts,
        "tool_call": view.model_dump() if view is not None else None,
    }


class ConversationDetailResponse(BaseModel):
    conversation: ConversationResponse
    messages: list[MessageResponse]


class ConversationUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@router.get("/conversations", response_model=list[ConversationSummaryResponse])
async def api_list_conversations(
    request: Request,
    channel: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    if q is not None and q.strip():
        convos = await conversations.search(db, query=q, channel=channel, limit=limit)
    else:
        convos = await conversations.list_all(db, channel=channel, limit=limit)
    out: list[dict[str, Any]] = []
    for c in convos:
        count = await conversations.message_count(db, c.id)
        item = _conversation_to_dict(c)
        item["message_count"] = count
        out.append(item)
    return out


@router.get("/conversations/{conv_id}", response_model=ConversationDetailResponse)
async def api_get_conversation(
    request: Request, conv_id: int, limit: int = 200
) -> dict[str, Any]:
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    convo = await conversations.get(db, conv_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    msgs = await messages.list_by_conversation(db, conv_id, limit=limit)
    return {
        "conversation": _conversation_to_dict(convo),
        "messages": [_message_to_dict(m) for m in msgs],
    }


@router.patch("/conversations/{conv_id}", response_model=ConversationResponse)
async def api_update_conversation(
    request: Request, conv_id: int, body: ConversationUpdateRequest
) -> dict[str, Any]:
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(status_code=400, detail="title must not be blank")

    db: AsyncEngine = request.app.state.db
    updated = await conversations.update_title(db, conv_id, title=title)
    if updated is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_to_dict(updated)


@router.delete("/conversations/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_conversation(request: Request, conv_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    deleted = await conversations.delete(
        db, conv_id, scratch_root=conversation_scratch_root()
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation not found")
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
    existing = await conversations.get(db, conv_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    updated = await conversations.set_bookmarked(
        db, conv_id, bookmarked=not existing.bookmarked
    )
    if updated is None:
        # Lost-the-race between get() and set_bookmarked().
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_to_dict(updated)


@router.post("/conversations/{conv_id}/retry")
async def api_retry_conversation(request: Request, conv_id: int) -> Response:
    """Regenerate the latest assistant response.

    Trims every assistant/tool turn that followed the last user message,
    then re-runs the web agent over the surviving context and streams the
    new reply with the same SSE semantics as /api/chat.
    """
    db: AsyncEngine = request.app.state.db

    convo = await conversations.get(db, conv_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    # Same channel guard as /api/chat: retry is a web-only surface.
    if convo.channel != WEB_CHANNEL:
        raise HTTPException(
            status_code=400,
            detail="conversation_id must reference a web conversation",
        )

    last_user = await messages.last_user_message(db, conv_id)
    if last_user is None:
        raise HTTPException(
            status_code=400,
            detail="conversation has no user message to retry",
        )

    # Drop the assistant/tool tail so run_agent regenerates from the same
    # context that produced the original reply (simplest persistence
    # strategy — no superseded_at bookkeeping).
    await messages.delete_after(db, conv_id, after_id=last_user.id)

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

    convo = await conversations.get(db, conv_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    # Same channel guard as /api/chat: edit is a web-only surface.
    if convo.channel != WEB_CHANNEL:
        raise HTTPException(
            status_code=400,
            detail="conversation_id must reference a web conversation",
        )

    target = await messages.get(db, message_id)
    # A message outside this conversation is a 404 (not found *here*), so the
    # path's conv_id is authoritative and clients can't edit across threads.
    if target is None or target.conversation_id != conv_id:
        raise HTTPException(status_code=404, detail="message not found")
    if target.role != "user":
        raise HTTPException(
            status_code=400, detail="only user messages can be edited"
        )

    updated = await messages.update_content(db, message_id, content=body.content)
    if updated is None:
        # Lost-the-race between the get() above and the update — the message
        # was deleted concurrently. Don't trim/regenerate on a phantom edit.
        raise HTTPException(status_code=404, detail="message not found")
    # Drop everything after the edited turn so run_agent regenerates from the
    # corrected context (simplest persistence strategy — no superseded_at).
    await messages.delete_after(db, conv_id, after_id=message_id)

    return await _stream_web_agent_run(request, convo)


# ---------------------------------------------------------------------------
# /api/notes
# ---------------------------------------------------------------------------


class NoteCreate(BaseModel):
    key: str = Field(min_length=1)
    content: str
    tags: list[str] = Field(default_factory=list)


class NoteUpdate(BaseModel):
    content: str
    tags: list[str] = Field(default_factory=list)


class NoteResponse(BaseModel):
    id: int
    key: str
    content: str
    tags: str | None
    updated_at: int


def _note_to_dict(n: Any) -> dict[str, Any]:
    return {
        "id": n.id,
        "key": n.key,
        "content": n.content,
        "tags": n.tags,
        "updated_at": n.updated_at,
    }


@router.get("/notes", response_model=list[NoteResponse])
async def api_list_notes(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    items = await notes.list_all(db, limit=limit)
    return [_note_to_dict(n) for n in items]


@router.get("/notes/{key}", response_model=NoteResponse)
async def api_get_note(request: Request, key: str) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    n = await notes.get(db, key)
    if n is None:
        raise HTTPException(status_code=404, detail="note not found")
    return _note_to_dict(n)


@router.post("/notes", response_model=NoteResponse)
async def api_create_note(request: Request, body: NoteCreate) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(db, key=body.key, content=body.content, tags=tags)
    return _note_to_dict(n)


@router.put("/notes/{key}", response_model=NoteResponse)
async def api_update_note(
    request: Request, key: str, body: NoteUpdate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(db, key=key, content=body.content, tags=tags)
    return _note_to_dict(n)


@router.delete("/notes/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_note(request: Request, key: str) -> Response:
    db: AsyncEngine = request.app.state.db
    if not await notes.delete(db, key):
        raise HTTPException(status_code=404, detail="note not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# /api/todos
# ---------------------------------------------------------------------------


class TodoCreate(BaseModel):
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class TodoUpdate(BaseModel):
    done: bool


class TodoResponse(BaseModel):
    id: int
    content: str
    tags: str | None
    done_at: int | None
    created_at: int


def _todo_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "content": t.content,
        "tags": t.tags,
        "done_at": t.done_at,
        "created_at": t.created_at,
    }


@router.get("/todos", response_model=list[TodoResponse])
async def api_list_todos(
    request: Request,
    only_open: bool = True,
    tag: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    items = await todos.list_all(db, only_open=only_open, tag=tag, limit=limit)
    return [_todo_to_dict(t) for t in items]


@router.post("/todos", response_model=TodoResponse)
async def api_create_todo(request: Request, body: TodoCreate) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    t = await todos.add(db, content=body.content, tags=tags)
    return _todo_to_dict(t)


@router.patch("/todos/{todo_id}", response_model=TodoResponse)
async def api_patch_todo(
    request: Request, todo_id: int, body: TodoUpdate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    if not body.done:
        raise HTTPException(
            status_code=400,
            detail="only marking todos as done is supported; pass {\"done\": true}",
        )
    if not await todos.mark_done(db, todo_id):
        # Either the row doesn't exist, or it was already done.
        existing = await todos.get(db, todo_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="todo not found")
        return _todo_to_dict(existing)
    t = await todos.get(db, todo_id)
    if t is None:
        raise HTTPException(status_code=404, detail="todo disappeared")
    return _todo_to_dict(t)


@router.delete("/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_todo(request: Request, todo_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    if not await todos.delete(db, todo_id):
        raise HTTPException(status_code=404, detail="todo not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# /api/reminders
# ---------------------------------------------------------------------------


class ReminderCreate(BaseModel):
    due_at: int
    message: str = Field(min_length=1)
    channel: str = "signal"


class ReminderResponse(BaseModel):
    id: int
    due_at: int
    message: str
    channel: str
    fired_at: int | None
    created_at: int


def _reminder_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "due_at": r.due_at,
        "message": r.message,
        "channel": r.channel,
        "fired_at": r.fired_at,
        "created_at": r.created_at,
    }


@router.get("/reminders", response_model=list[ReminderResponse])
async def api_list_reminders(
    request: Request, include_fired: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    items = await reminders.list_all(db, include_fired=include_fired, limit=limit)
    return [_reminder_to_dict(r) for r in items]


@router.post("/reminders", response_model=ReminderResponse)
async def api_create_reminder(
    request: Request, body: ReminderCreate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    r = await reminders.create(
        db, due_at=body.due_at, message=body.message, channel=body.channel
    )
    return _reminder_to_dict(r)


@router.delete("/reminders/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_reminder(request: Request, reminder_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    if not await reminders.delete(db, reminder_id):
        raise HTTPException(status_code=404, detail="reminder not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
