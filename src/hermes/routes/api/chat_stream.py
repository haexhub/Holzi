"""SSE streaming engine for the web-channel agent.

Shared by `POST /api/chat`, `POST /api/conversations/{id}/retry`, and
`POST /api/conversations/{id}/messages/{message_id}/edit-and-regenerate`
so retry/edit-and-regenerate are not separate code paths."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import ApprovalDecision, ChatRunCancelled, run_agent
from hermes.auth import current_user_id
from hermes.config import settings
from hermes.events import (
    ApprovalRequiredData,
    ApprovalRequiredEvent,
    CancelledEvent,
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
from hermes.personas import resolve_persona_context
from hermes.repository import (
    approvals as approvals_repo,
)
from hermes.repository import (
    conversations,
)
from hermes.repository import (
    skills as skills_repo,
)
from hermes.run_tracker import track_run
from hermes.sandbox import WorkspaceCrash
from hermes.tool_catalog import build_tool_catalog
from hermes.upstream import build_client_for_credential

WEB_CHANNEL = "web"
CLINE_CHANNEL = "cline"

# Approvals can take minutes; idle proxies (Traefik, mobile carriers) close
# silent SSE connections. Emit a comment heartbeat at this cadence whenever no
# real event is flowing so the connection stays warm while we wait.
SSE_HEARTBEAT_SECONDS = 15.0


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
    uid = current_user_id(request)

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
        user_id=current_user_id(request),
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
            if await approvals_repo.is_always_allowed(db, name, user_id=uid):
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
                await approvals_repo.grant_always(db, name, user_id=uid)
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
                    user_id=current_user_id(request),
                    conversation_id=convo.id,
                    channel=WEB_CHANNEL,
                    model=model,
                    metrics=metrics,
                ):
                    await run_agent(
                        upstream=persona_upstream,
                        db=db,
                        user_id=current_user_id(request),
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
