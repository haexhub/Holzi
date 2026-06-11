import asyncio
import contextlib
import json
import re
import uuid
import zoneinfo
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import attachments as attachments_mod
from hermes.agent import ApprovalDecision, ChatRunCancelled, run_agent
from hermes.auth import current_user_id
from hermes.config import conversation_scratch_root, settings
from hermes.errors import ErrorCode
from hermes.events import (
    ApprovalDecisionLiteral,
    ApprovalRequiredData,
    ApprovalRequiredEvent,
    CancelledEvent,
    ChatStreamEnvelope,
    DoneEvent,
    ErrorData,
    ErrorEvent,
    ReasoningData,
    ReasoningEvent,
    RunData,
    RunEvent,
    SandboxCrashedData,
    SandboxCrashedEvent,
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
from hermes.personas import resolve_chat_context_meta, resolve_persona_context
from hermes.provider_models import ProviderModelsError, list_provider_models
from hermes.repository import (
    agent_tasks,
    attachments,
    conversations,
    messages,
    notes,
    runs,
)
from hermes.repository import (
    approvals as approvals_repo,
)
from hermes.repository import (
    llm_credentials as llm_credentials_repo,
)
from hermes.repository import (
    skills as skills_repo,
)
from hermes.run_tracker import track_run
from hermes.sandbox import WorkspaceCrash
from hermes.thinking import resolve_thinking_support
from hermes.tool_catalog import build_tool_catalog
from hermes.upstream import build_client_for_credential

router = APIRouter(prefix="/api")

WEB_CHANNEL = "web"
CLINE_CHANNEL = "cline"

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
            detail={
                "code": ErrorCode.REQUEST_LIMIT_OUT_OF_RANGE.value,
                "params": {"min": 1, "max": max_limit},
            },
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


_API_KEY_RE = re.compile(
    r"\b("
    r"sk-ant-[A-Za-z0-9_\-]{20,}"   # Anthropic
    r"|sk-[A-Za-z0-9_\-]{20,}"       # OpenAI (incl. sk-proj-… project keys)
    r"|gsk_[A-Za-z0-9]{20,}"         # Google AI Studio
    r"|AIza[A-Za-z0-9_\-]{35,}"      # Google
    r")\b"
    r"|Bearer [A-Za-z0-9_\-\.]{20,}" # Generic bearer
)


def _sanitize_upstream_message(body: bytes, status: int) -> str:
    """Extract a safe-to-display message from a provider error response body.

    Parses JSON, extracts .error.message or top-level .message, redacts
    known API key patterns, and truncates to 300 chars. Returns
    "HTTP <status>" on parse failure or empty body.
    """
    if not body:
        return f"HTTP {status}"
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return f"HTTP {status}"
    msg = ""
    if isinstance(data.get("error"), dict):
        msg = str(data["error"].get("message", ""))
    if not msg:
        msg = str(data.get("message", ""))
    if not msg:
        return f"HTTP {status}"
    msg = _API_KEY_RE.sub("[REDACTED]", msg)
    return msg[:300]


def _classify_chat_error(exc: BaseException) -> tuple[str, int, str]:
    """Map an agent-loop exception to (sse_code, status_code, message).

    status_code is the HTTP status the upstream actually returned (or the
    equivalent synthetic one for network errors). The frontend uses it to
    distinguish 429 (rate-limit) from 5xx (provider error) from 50x (our side).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        upstream_status = exc.response.status_code
        try:
            body = exc.response.content  # populated for non-streaming raises
        except httpx.ResponseNotRead:
            body = b""
        message = _sanitize_upstream_message(body, upstream_status)
        if upstream_status == 429:
            return ("upstream_rate_limited", 429, message)
        return ("upstream_http_error", upstream_status, message)
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
    if payload.attachment_ids:
        for aid in payload.attachment_ids:
            att = await attachments.get(db, aid)
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
        db, conversation_id=convo.id, role="user", content=payload.message
    )
    if payload.attachment_ids:
        await attachments.link_to_message(
            db,
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


async def _stream_web_agent_run(
    request: Request,
    convo: Any,
    *,
    model_override: str | None = None,
    persona_id_override: int | None = None,
    thinking_budget: Literal["low", "medium", "high"] | None = None,
    skill_hints: list[str] | None = None,
) -> Response:
    """Run the web agent over the conversation's current message history and
    stream it as SSE. Shared by /api/chat (after appending the new user
    message) and /api/conversations/{id}/retry (after trimming the trailing
    assistant/tool tail) so retry is not a separate code path."""
    db: AsyncEngine = request.app.state.db

    tools = build_tool_catalog(
        db=db,
        external_http=request.app.state.external_http,
        brave_api_key=request.app.state.brave_api_key,
        mcp_manager=request.app.state.mcp_servers_manager,
        encryptor=request.app.state.encryptor,
        tool_catalog_provider=lambda: request.app.state.tool_catalog,
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
    # Plan 21: per-conversation set of tools the user has granted
    # `allow_session` to. Always-scope grants live in the `tool_approvals`
    # table — this dict only holds the in-memory session view.
    session_approvals: dict[int, set[str]] = (
        request.app.state.session_approvals
    )

    # Resolve persona context once before the SSE generator so the model id
    # we persist in agent_runs matches what the upstream actually saw.
    persona_ctx = await resolve_persona_context(
        WEB_CHANNEL,
        db,
        model_override=model_override,
        persona_id_override=persona_id_override,
    )
    model = persona_ctx.model

    if skill_hints:
        hinted = [s for s in await skills_repo.list_all(db) if s.slug in skill_hints]
        if hinted:
            skill_blocks = "\n\n".join(
                f"## Skill: {s.name}\n\n{s.body_markdown}" for s in hinted
            )
            persona_ctx.system_prompt = skill_blocks + "\n\n" + persona_ctx.system_prompt

    persona_upstream = build_client_for_credential(
        persona_ctx.credential,
        encryptor=request.app.state.encryptor,
        fallback_proxy_url=settings.llm_url,
    )

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

        async def on_reasoning(chunk: str) -> None:
            await queue.put(ReasoningEvent(data=ReasoningData(content=chunk)))

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
            # Plan 21 pre-gate: if the user has already granted this tool a
            # standing permission (always-scope persisted in DB, or
            # session-scope cached for the active conversation), skip the
            # card entirely. The agent loop only branches on
            # `decision == "deny"`, so returning `allow_once` is the right
            # signal regardless of which standing scope matched.
            if await approvals_repo.is_always_allowed(db, name):
                return ApprovalDecision(decision="allow_once")
            if name in session_approvals.get(convo.id, set()):
                return ApprovalDecision(decision="allow_once")

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
                decision = await future
            finally:
                # Drop the entry however we leave (resolved, run cancelled,
                # client disconnect) so a later decision can't hit a stale id.
                approvals.pop(approval_id, None)
            # Promote the decision to a standing grant when the user picked a
            # broader scope than `allow_once`. The agent loop sees the
            # original `decision` so its `== "deny"` check is unchanged.
            if decision.decision == "allow_session":
                session_approvals.setdefault(convo.id, set()).add(name)
            elif decision.decision == "allow_always":
                await approvals_repo.grant_always(db, name)
            return decision

        # Subscribe to sandbox crashes for the lifetime of this stream so a
        # workspace dying mid-conversation surfaces as a `sandbox_crashed`
        # event the UI can render with a Restart action. Surface-only: the
        # health watcher never auto-restarts.
        sandbox_manager = request.app.state.sandbox_manager

        async def on_sandbox_crash(crash: WorkspaceCrash) -> None:
            await queue.put(
                SandboxCrashedEvent(
                    data=SandboxCrashedData(
                        workspace_id=crash.workspace_id,
                        sandbox_id=crash.sandbox_id,
                        state=crash.state.value,  # type: ignore[arg-type]
                        exit_code=crash.exit_code,
                    )
                )
            )

        if sandbox_manager is not None:
            sandbox_manager.add_crash_handler(on_sandbox_crash)

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
                        upstream=persona_upstream,
                        db=db,
                        conversation_id=convo.id,
                        system_prompt=persona_ctx.system_prompt,
                        model=model,
                        tools=tools,
                        on_chunk=on_chunk,
                        on_reasoning=on_reasoning,
                        on_tool_call=on_tool_call,
                        on_tool_result=on_tool_result,
                        on_approval=on_approval,
                        cancel_event=cancel_event,
                        metrics=metrics,
                        thinking_budget=thinking_budget,
                        provider=persona_ctx.credential.provider,
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
            await conversations.touch(db, convo.id, user_id=current_user_id(request))
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
            if sandbox_manager is not None:
                sandbox_manager.remove_crash_handler(on_sandbox_crash)
            await persona_upstream.aclose()

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
        WEB_CHANNEL, db
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
    credentials = await llm_credentials_repo.list_all(db)
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


# ---------------------------------------------------------------------------
# POST /api/approvals/{approval_id} — resolve a paused, approval-gated tool.
# ---------------------------------------------------------------------------


class ApprovalDecisionRequest(BaseModel):
    # Plan 21: four-decision union. Backend interprets `allow_session` /
    # `allow_always` to upsert the standing lists; the agent loop only
    # cares about `deny` vs. not-deny.
    decision: ApprovalDecisionLiteral
    # Optional note shown to the LLM on deny so it can adapt its next turn.
    # Length-capped at 500 to keep tool errors bounded and to stop a stray
    # paste from blowing the request body up; the UI hint matches.
    reason: str | None = Field(default=None, max_length=500)


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
        raise HTTPException(
            status_code=404, detail=ErrorCode.APPROVAL_NOT_FOUND.value
        )
    if future.done():
        raise HTTPException(
            status_code=409, detail=ErrorCode.APPROVAL_ALREADY_RESOLVED.value
        )
    future.set_result(
        ApprovalDecision(decision=body.decision, reason=body.reason)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Plan 21 standing-approvals surface: read + revoke the two non-`allow_once`
# scopes. Granting still goes through the approval card (POST above) — a
# dedicated grant endpoint would just duplicate that flow.
# ---------------------------------------------------------------------------


class StandingAlwaysEntry(BaseModel):
    tool: str
    granted_at: int
    last_used_at: int | None


class StandingSessionEntry(BaseModel):
    conversation_id: int
    tool: str


class StandingApprovalsResponse(BaseModel):
    always: list[StandingAlwaysEntry]
    session: list[StandingSessionEntry]


@router.get("/approvals/standing", response_model=StandingApprovalsResponse)
async def api_list_standing_approvals(request: Request) -> StandingApprovalsResponse:
    """Read the active standing-approval lists.

    Returns persisted `allow_always` rows plus the in-memory
    `allow_session` entries currently cached on `app.state`. The
    web UI doesn't have a dedicated settings page for these yet
    (Plan 21 Non-Goal); the data shape is here so a future page
    only has to render it.
    """
    db: AsyncEngine = request.app.state.db
    always_rows = await approvals_repo.list_always(db)
    session_state: dict[int, set[str]] = request.app.state.session_approvals
    session_entries = sorted(
        (
            StandingSessionEntry(conversation_id=conv_id, tool=tool)
            for conv_id, tools in session_state.items()
            for tool in tools
        ),
        key=lambda e: (e.conversation_id, e.tool),
    )
    return StandingApprovalsResponse(
        always=[
            StandingAlwaysEntry(
                tool=row.tool_name,
                granted_at=row.granted_at,
                last_used_at=row.last_used_at,
            )
            for row in always_rows
        ],
        session=session_entries,
    )


StandingScope = Literal["always", "session"]


@router.delete(
    "/approvals/standing/{tool_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Tool not in the standing list for that scope"}},
)
async def api_revoke_standing_approval(
    request: Request, tool_name: str, scope: StandingScope
) -> Response:
    """Drop a standing-approval grant so the next call re-prompts.

    `scope=always` deletes the `tool_approvals` row; `scope=session`
    removes the tool from every conversation's in-memory set (single-user
    deployment — there's only one user's app.state to scrub). Missing
    tools 404 so the UI can tell a stale revoke from a successful one.
    """
    if scope == "always":
        db: AsyncEngine = request.app.state.db
        removed = await approvals_repo.revoke_always(db, tool_name)
        if not removed:
            raise HTTPException(
                status_code=404, detail=ErrorCode.TOOL_NOT_IN_ALWAYS_ALLOWED.value
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # scope == "session": single user / single-worker deployment, so we
    # walk every conversation's set. Conversations whose set becomes empty
    # get dropped to keep the dict tidy.
    session_state: dict[int, set[str]] = request.app.state.session_approvals
    touched = False
    for conv_id in list(session_state.keys()):
        tools = session_state[conv_id]
        if tool_name in tools:
            tools.discard(tool_name)
            touched = True
            if not tools:
                session_state.pop(conv_id, None)
    if not touched:
        raise HTTPException(
            status_code=404, detail=ErrorCode.TOOL_NOT_IN_SESSION_ALLOWED.value
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
        raise HTTPException(
            status_code=400, detail=ErrorCode.REQUEST_INVALID_OFFSET.value
        )
    if status is not None and status not in runs.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.REQUEST_INVALID_STATUS.value,
                "params": {"allowed": sorted(runs.VALID_STATUSES)},
            },
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
    db: AsyncEngine, conv_id: int, *, after_id: int
) -> None:
    """Delete the on-disk blobs of attachments linked to messages after
    `after_id`. Their DB rows are removed by the messages CASCADE when the
    caller trims those turns; this reclaims the files so they don't leak in
    the scratch dir until the whole conversation is deleted."""
    leaked = await attachments.list_after_message(
        db, conversation_id=conv_id, after_message_id=after_id
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
    limit = _validate_limit(limit)
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
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    convo = await conversations.get(db, conv_id, user_id=current_user_id(request))
    if convo is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )
    msgs = await messages.list_by_conversation(db, conv_id, limit=limit)
    atts_by_message: dict[int, list[Any]] = {}
    for att in await attachments.list_by_conversation(db, conv_id):
        if att.message_id is not None:
            atts_by_message.setdefault(att.message_id, []).append(att)
    return {
        "conversation": _conversation_to_dict(convo),
        "messages": [
            _message_to_dict(m, atts_by_message.get(m.id)) for m in msgs
        ],
    }


@router.post(
    "/conversations/{conv_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def api_upload_attachment(
    request: Request,
    conv_id: int,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI dependency default
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    convo = await conversations.get(db, conv_id, user_id=current_user_id(request))
    if convo is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.CONVERSATION_NOT_FOUND.value
        )

    content_type = file.content_type or "application/octet-stream"
    if not attachments_mod.is_allowed(content_type):
        raise HTTPException(
            status_code=415,
            detail={
                "code": ErrorCode.ATTACHMENT_UNSUPPORTED_TYPE.value,
                "params": {"type": content_type},
            },
        )

    # Read in chunks so an oversized body is rejected without buffering the
    # whole thing in memory (Starlette already spools to disk past 1 MB).
    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > attachments_mod.MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": ErrorCode.ATTACHMENT_TOO_LARGE.value,
                    "params": {
                        "max_bytes": attachments_mod.MAX_ATTACHMENT_BYTES,
                    },
                },
            )

    # On-disk name is an opaque token, so the user-supplied filename can
    # never influence the path (no traversal possible).
    token = uuid.uuid4().hex
    target_dir = attachments_mod.attachment_dir(conv_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / token).write_bytes(bytes(data))

    att = await attachments.create(
        db,
        conversation_id=conv_id,
        filename=attachments_mod.safe_display_filename(file.filename),
        content_type=content_type,
        size=len(data),
        storage_path=token,
    )
    return _attachment_to_dict(att)


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

    convo = await conversations.get(db, conv_id, user_id=current_user_id(request))
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

    last_user = await messages.last_user_message(db, conv_id)
    if last_user is None:
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.CONVERSATION_NO_USER_MESSAGE_TO_RETRY.value,
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

    convo = await conversations.get(db, conv_id, user_id=current_user_id(request))
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

    target = await messages.get(db, message_id)
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

    updated = await messages.update_content(db, message_id, content=body.content)
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
    await _unlink_attachment_files_after(db, conv_id, after_id=message_id)
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


def _fts5_query(raw: str) -> str:
    # FTS5 treats `"`, `:`, `*`, `(`, `)`, `-`, etc. as syntax, so a user's
    # free-form search string would otherwise raise OperationalError at the DB
    # boundary. Split on whitespace, drop any non-alnum chars per token, and
    # quote each surviving token as a phrase — that gives multi-term AND
    # matching without exposing FTS5 operator syntax to the UI.
    tokens: list[str] = []
    for raw_token in raw.split():
        cleaned = "".join(c for c in raw_token if c.isalnum() or c == "_")
        if cleaned:
            tokens.append(f'"{cleaned}"')
    return " ".join(tokens)


@router.get("/notes", response_model=list[NoteResponse])
async def api_list_notes(
    request: Request, limit: int = 100, q: str | None = None
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    user_id = current_user_id(request)
    # Whitespace-only `q` is treated the same as an absent `q` — falling
    # through to list_all keeps `?q=` and `?q=%20%20` symmetric.
    if q and q.strip():
        sanitised = _fts5_query(q)
        if not sanitised:
            return []
        items = await notes.find(db, user_id=user_id, query=sanitised, limit=limit)
    else:
        items = await notes.list_all(db, user_id=user_id, limit=limit)
    return [_note_to_dict(n) for n in items]


@router.get("/notes/{key}", response_model=NoteResponse)
async def api_get_note(request: Request, key: str) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    n = await notes.get(db, key, user_id=current_user_id(request))
    if n is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.NOTE_NOT_FOUND.value
        )
    return _note_to_dict(n)


@router.post("/notes", response_model=NoteResponse)
async def api_create_note(request: Request, body: NoteCreate) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(
        db, user_id=current_user_id(request), key=body.key, content=body.content, tags=tags
    )
    return _note_to_dict(n)


@router.put("/notes/{key}", response_model=NoteResponse)
async def api_update_note(
    request: Request, key: str, body: NoteUpdate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    tags = ",".join(body.tags) if body.tags else None
    n = await notes.upsert(
        db, user_id=current_user_id(request), key=key, content=body.content, tags=tags
    )
    return _note_to_dict(n)


@router.delete("/notes/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_note(request: Request, key: str) -> Response:
    db: AsyncEngine = request.app.state.db
    if not await notes.delete(db, key, user_id=current_user_id(request)):
        raise HTTPException(
            status_code=404, detail=ErrorCode.NOTE_NOT_FOUND.value
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# /api/tasks (Plan 16) — scheduled and one-shot agent runs.
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1)
    due_at: int | None = None
    schedule: str | None = None
    timezone: str = "UTC"
    enabled: bool = True


class TaskUpdate(BaseModel):
    # Every field is optional — only sent fields are patched. `due_at` /
    # `schedule` use the explicit "set to null" semantics via separate
    # `clear_*` flags so a missing key on the wire can't accidentally clear
    # the other half of the (exactly-one) invariant.
    title: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1)
    due_at: int | None = None
    clear_due_at: bool = False
    schedule: str | None = None
    clear_schedule: bool = False
    timezone: str | None = None
    enabled: bool | None = None


class TaskResponse(BaseModel):
    id: int
    title: str
    prompt: str
    due_at: int | None
    schedule: str | None
    timezone: str
    enabled: bool
    last_run_at: int | None
    last_status: str | None
    last_run_id: str | None
    created_at: int
    updated_at: int


class TaskRunResponse(BaseModel):
    """Returned from POST /api/tasks/{id}/run.

    The run is fire-and-forget: by the time this returns 202, the scheduler
    background task is queued but the `agent_runs` row may not exist yet.
    Clients see the resulting `last_run_id` via the next `GET /api/tasks/{id}`
    once the run is recorded. We don't pre-allocate a run id here because the
    scheduler mints its own (and we'd have to thread it through three layers
    just so the response could carry a string the UI could already poll for).
    """

    task_id: int
    status: Literal["queued"]


def _task_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "prompt": t.prompt,
        "due_at": t.due_at,
        "schedule": t.schedule,
        "timezone": t.timezone,
        "enabled": t.enabled,
        "last_run_at": t.last_run_at,
        "last_status": t.last_status,
        "last_run_id": t.last_run_id,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


def _validate_timezone(tz: str) -> None:
    """Surface unknown IANA tz names as a 400 instead of a 500. `zoneinfo`
    raises `ZoneInfoNotFoundError` (a subclass of KeyError) deep inside
    cron evaluation; without this guard the user sees an opaque server
    error for a perfectly client-side mistake."""
    try:
        zoneinfo.ZoneInfo(tz)
    except zoneinfo.ZoneInfoNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.TASK_UNKNOWN_TIMEZONE.value,
                "params": {"tz": tz},
            },
        ) from exc


def _validate_task_schedule_payload(
    *, due_at: int | None, schedule: str | None
) -> None:
    """Enforce the exactly-one-of invariant at the API boundary so the
    repository layer's ValueError surfaces as a 400 instead of a 500.
    """
    if (due_at is None) == (schedule is None):
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.TASK_DUE_OR_SCHEDULE_REQUIRED.value,
        )
    if schedule is not None:
        try:
            agent_tasks.validate_schedule(schedule)
        except ValueError as exc:
            raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.INVALID_REQUEST.value,
                "params": {"message": str(exc)},
            },
        ) from exc


@router.get("/tasks", response_model=list[TaskResponse])
async def api_list_tasks(
    request: Request, limit: int = 200
) -> list[dict[str, Any]]:
    limit = _validate_limit(limit)
    db: AsyncEngine = request.app.state.db
    items = await agent_tasks.list_all(db, limit=limit)
    return [_task_to_dict(t) for t in items]


@router.post(
    "/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def api_create_task(
    request: Request, body: TaskCreate
) -> dict[str, Any]:
    _validate_task_schedule_payload(due_at=body.due_at, schedule=body.schedule)
    _validate_timezone(body.timezone)
    db: AsyncEngine = request.app.state.db
    t = await agent_tasks.create(
        db,
        title=body.title,
        prompt=body.prompt,
        due_at=body.due_at,
        schedule=body.schedule,
        timezone=body.timezone,
        enabled=body.enabled,
    )
    return _task_to_dict(t)


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
async def api_patch_task(
    request: Request, task_id: int, body: TaskUpdate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    # Validate cron + tz up front so the repository's ValueError /
    # ZoneInfoNotFoundError surfaces as a useful 400 instead of a 500.
    if body.schedule is not None:
        try:
            agent_tasks.validate_schedule(body.schedule)
        except ValueError as exc:
            raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.INVALID_REQUEST.value,
                "params": {"message": str(exc)},
            },
        ) from exc
    if body.timezone is not None:
        _validate_timezone(body.timezone)
    try:
        updated = await agent_tasks.update(
            db,
            task_id,
            title=body.title,
            prompt=body.prompt,
            due_at=body.due_at,
            schedule=body.schedule,
            timezone=body.timezone,
            enabled=body.enabled,
            clear_due_at=body.clear_due_at,
            clear_schedule=body.clear_schedule,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.INVALID_REQUEST.value,
                "params": {"message": str(exc)},
            },
        ) from exc
    if updated is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.TASK_NOT_FOUND.value
        )
    return _task_to_dict(updated)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_delete_task(request: Request, task_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    if not await agent_tasks.delete(db, task_id):
        raise HTTPException(
            status_code=404, detail=ErrorCode.TASK_NOT_FOUND.value
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tasks/{task_id}/run",
    response_model=TaskRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def api_run_task_now(
    request: Request, task_id: int
) -> dict[str, Any]:
    """Fire a task immediately as a background job; respond 202 so the
    client knows the run was accepted. The resulting `agent_runs` row id
    lands on the task's `last_run_id` once the scheduler records it —
    clients poll `GET /api/tasks/{id}` to pick it up. Does NOT advance the
    cron schedule — a manual run shouldn't skip the next due occurrence.
    """
    db: AsyncEngine = request.app.state.db
    task = await agent_tasks.get(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.TASK_NOT_FOUND.value
        )

    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(
            status_code=503, detail=ErrorCode.TASK_SCHEDULER_NOT_CONFIGURED.value
        )

    asyncio.create_task(
        _run_task_background(scheduler, task_id),
        name=f"task-run-now-{task_id}",
    )
    return {"task_id": task_id, "status": "queued"}


async def _run_task_background(scheduler: Any, task_id: int) -> None:
    """Run a task in the background. Any error is logged but never raised
    — the API has already returned 202, so there's no caller to surface
    to. The user sees the failure via `last_status` on the next list refresh.
    """
    try:
        await scheduler.run_now(task_id)
    except LookupError:
        logger.warning("api_task_run_now_missing", task_id=task_id)
    except Exception:  # noqa: BLE001 — already persisted as last_status
        logger.exception("api_task_run_now_failed", task_id=task_id)


# --- workspace sandbox (Plan 11b-b) -----------------------------------------
# These expose the SandboxManager so the UI can show liveness and offer the
# Restart action behind the `sandbox_crashed` event. The sandbox manager is
# only present when the agent is configured with a Podman socket; without it
# these endpoints return 503 so the caller can fall back gracefully.


class SandboxStatusResponse(BaseModel):
    workspace_id: str
    state: Literal["running", "exited", "crashed", "oom", "removed", "absent"]
    exit_code: int | None = None


def _require_sandbox_manager(request: Request) -> Any:
    mgr = request.app.state.sandbox_manager
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorCode.SANDBOX_NOT_CONFIGURED.value,
        )
    return mgr


@router.get(
    "/workspaces/{workspace_id}/sandbox",
    response_model=SandboxStatusResponse,
)
async def api_get_sandbox_status(
    request: Request, workspace_id: str
) -> dict[str, Any]:
    mgr = _require_sandbox_manager(request)
    handle = mgr.peek_workspace(workspace_id)
    if handle is None:
        # No sandbox has been spun up for this workspace yet. "absent" is
        # distinct from "removed" (which means we *had* one and it's gone).
        return {"workspace_id": workspace_id, "state": "absent", "exit_code": None}
    status_value = await mgr.status(handle)
    return {
        "workspace_id": workspace_id,
        "state": status_value.state.value,
        "exit_code": status_value.exit_code,
    }


@router.post(
    "/workspaces/{workspace_id}/sandbox/restart",
    response_model=SandboxStatusResponse,
)
async def api_restart_sandbox(
    request: Request, workspace_id: str
) -> dict[str, Any]:
    mgr = _require_sandbox_manager(request)
    handle = await mgr.restart_workspace(workspace_id)
    status_value = await mgr.status(handle)
    return {
        "workspace_id": workspace_id,
        "state": status_value.state.value,
        "exit_code": status_value.exit_code,
    }
