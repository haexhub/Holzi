import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import messages

CHAT_PATH = "/v1/chat/completions"

OnChunk = Callable[[str], Awaitable[None]]
# (call_id, tool_name, parsed_arguments)
OnToolCall = Callable[[str, str, dict[str, Any]], Awaitable[None]]
# (call_id, status["success"|"error"], result_or_error_text)
OnToolResult = Callable[[str, str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """The user's verdict on a paused tool call. ``reason`` is an optional
    free-text note (shown to the LLM on deny so it can adapt)."""

    decision: Literal["allow_once", "deny"]
    reason: str | None = None


# (call_id, tool_name, parsed_arguments, risk_reason) -> decision. Called before
# a `requires_approval` tool runs; the web layer emits an `approval_required`
# SSE event and blocks on the user's POST. When no callback is supplied (Signal
# / MCP), the gate is skipped and the tool runs normally.
OnApproval = Callable[[str, str, dict[str, Any], str], Awaitable[ApprovalDecision]]


class ChatRunCancelled(Exception):
    """Raised when run_agent observes its cancel_event between steps.

    Distinct from `asyncio.CancelledError`: cancellation here is a
    cooperative user-initiated abort signalled via an `asyncio.Event`,
    not a task-level cancel. The web layer maps this to a `cancelled`
    SSE event and skips persisting a final assistant turn. The in-memory
    run registry that backs the event lives on `app.state` and assumes
    the single-worker / single-user deployment invariant documented at
    the top of this module.
    """


# Single-worker invariant:
# The /api/chat cancel feature stores per-request cancellation events in an
# in-memory dict on `app.state` (see routes/api.py). That mapping is only
# correct if every request for a given run_id lands in the same Python
# process. The deployment model is "one container = one user = one
# uvicorn worker" (see docs/plans/holzi-agent-parity/README.md), so this
# holds in production. `main.py` refuses to start when WEB_CONCURRENCY /
# UVICORN_WORKERS / GUNICORN_WORKERS asks for >1 worker. If you ever
# need to scale horizontally, move the registry to Redis (or similar)
# before lifting that check.


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]
    # When true, run_agent pauses before executing this tool and asks the
    # caller's `on_approval` callback for a decision. `risk_reason` is the
    # human-readable explanation shown on the approval card.
    requires_approval: bool = False
    risk_reason: str | None = None


def _raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ChatRunCancelled()


async def run_agent(
    *,
    upstream: httpx.AsyncClient,
    db: AsyncEngine,
    conversation_id: int,
    system_prompt: str,
    model: str,
    tools: list[Tool] | None = None,
    max_iterations: int = 10,
    on_chunk: OnChunk | None = None,
    on_tool_call: OnToolCall | None = None,
    on_tool_result: OnToolResult | None = None,
    on_approval: OnApproval | None = None,
    cancel_event: asyncio.Event | None = None,
    metrics: dict[str, Any] | None = None,
) -> str:
    """Run a single agent turn until the assistant stops requesting tool calls.

    History is loaded from the DB (the caller is expected to have persisted
    the new user message already). Each LLM call is one iteration; tool
    results are persisted and fed back as the next request. Returns the
    final assistant text.

    When `on_chunk` is given, the upstream request is sent with
    `stream=True` and every `delta.content` is forwarded to the callback
    incrementally — useful for the web-UI's SSE stream. Tool-call rounds
    still produce no visible text chunks (the LLM emits empty content
    deltas alongside the tool_call deltas). When `on_chunk` is None the
    non-streaming JSON path is used (Signal worker, MCP, tests).

    If `cancel_event` is supplied and gets set during the run, the agent
    raises `ChatRunCancelled` at the next safe check (before/after
    upstream, before/after tool execution). No final assistant message is
    persisted in that case — but tool rounds that completed before
    cancellation remain in history, since the conversation truly contains
    those side effects.
    """
    history = await messages.list_by_conversation(db, conversation_id)
    request_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for m in history:
        request_messages.append(_history_row_to_request_message(m))

    tools_payload = _format_tools(tools) if tools else None
    tool_lookup = {t.name: t for t in tools} if tools else {}

    for _ in range(max_iterations):
        _raise_if_cancelled(cancel_event)

        body: dict[str, Any] = {"model": model, "messages": request_messages}
        if tools_payload:
            body["tools"] = tools_payload

        if on_chunk is None:
            assistant_text, tool_calls = await _request_round_nonstream(
                upstream, body, metrics
            )
        else:
            body["stream"] = True
            # OpenAI-compatible streams only emit the terminal `usage`
            # chunk when this is set; without it metrics stays empty
            # against a real provider (our test mock injects usage
            # unconditionally, so this isn't caught there).
            if metrics is not None:
                body["stream_options"] = {"include_usage": True}
            assistant_text, tool_calls = await _request_round_stream(
                upstream, body, on_chunk, cancel_event, metrics
            )

        _raise_if_cancelled(cancel_event)

        if not tool_calls:
            await messages.append(
                db, conversation_id=conversation_id, role="assistant", content=assistant_text
            )
            return assistant_text

        # Assistant turn that requested tools — persist with the tool_calls
        # in meta_json so we can replay later if needed.
        request_messages.append(
            {
                "role": "assistant",
                "content": assistant_text,
                "tool_calls": tool_calls,
            }
        )
        await messages.append(
            db,
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_text,
            meta_json=json.dumps({"tool_calls": tool_calls}),
        )

        for call in tool_calls:
            _raise_if_cancelled(cancel_event)
            name = call["function"]["name"]
            args, arg_error = _parse_tool_arguments(call)
            tool = tool_lookup.get(name)

            # Approval gate: a requires_approval tool pauses here until the
            # caller's on_approval callback returns a decision. Skipped when
            # no callback is wired (Signal/MCP) or the arguments were already
            # malformed (let the normal error path handle that). On deny we
            # never run the tool — a denied result is fed back to the LLM.
            denied_reason: str | None = None
            denied = False
            if (
                on_approval is not None
                and tool is not None
                and tool.requires_approval
                and arg_error is None
            ):
                decision = await on_approval(
                    call["id"], name, args, tool.risk_reason or ""
                )
                _raise_if_cancelled(cancel_event)
                if decision.decision == "deny":
                    denied = True
                    denied_reason = decision.reason

            if denied:
                status, result = "error", _denied_tool_result(denied_reason)
            else:
                if on_tool_call is not None:
                    await on_tool_call(call["id"], name, args)
                status, result = await _execute_tool_call(
                    call, tool_lookup, args, arg_error
                )
                _raise_if_cancelled(cancel_event)
                if on_tool_result is not None:
                    await on_tool_result(call["id"], status, result)
            request_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                }
            )
            await messages.append(
                db,
                conversation_id=conversation_id,
                role="tool",
                content=result,
                meta_json=json.dumps(
                    {
                        "tool_call_id": call["id"],
                        "name": name,
                        "arguments": args,
                        "status": status,
                    }
                ),
            )

    raise RuntimeError(f"agent loop exceeded max_iterations={max_iterations}")


async def _request_round_nonstream(
    upstream: httpx.AsyncClient,
    body: dict[str, Any],
    metrics: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    resp = await upstream.post(CHAT_PATH, json=body)
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]
    text = str(msg.get("content") or "")
    tool_calls = list(msg.get("tool_calls") or [])
    _accumulate_usage(metrics, data.get("usage"))
    return text, tool_calls


async def _request_round_stream(
    upstream: httpx.AsyncClient,
    body: dict[str, Any],
    on_chunk: OnChunk,
    cancel_event: asyncio.Event | None = None,
    metrics: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    # Indexed by `delta.tool_calls[i].index` because each call's fields
    # arrive split across chunks.
    tool_calls_by_idx: dict[int, dict[str, Any]] = {}
    # Terminal markers are either an explicit `data: [DONE]` line (OpenAI
    # convention) or a chunk whose first choice carries a non-null
    # `finish_reason` (which some compatible providers emit instead).
    # Without one, refuse to persist a half-built turn.
    saw_terminal_marker = False

    async with upstream.stream("POST", CHAT_PATH, json=body) as resp:
        resp.raise_for_status()
        async for raw_line in resp.aiter_lines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                saw_terminal_marker = True
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # OpenAI-style usage block arrives in a terminal chunk whose
            # `choices` is empty and `usage` is populated (only when
            # stream_options.include_usage=true). Capture it before the
            # empty-choices guard skips the chunk.
            _accumulate_usage(metrics, obj.get("usage"))
            choices = obj.get("choices") or []
            if not choices:
                continue
            if choices[0].get("finish_reason") is not None:
                saw_terminal_marker = True
            delta = choices[0].get("delta") or {}

            content = delta.get("content")
            if content:
                text_parts.append(content)
                await on_chunk(content)

            for tc in delta.get("tool_calls") or []:
                _accumulate_tool_call(tool_calls_by_idx, tc)

            # Cooperative cancel point: check after each delta so the user
            # can stop a still-running stream without waiting for upstream
            # to finish. We don't try to break out mid-network-read — if
            # the upstream stalls, the client disconnect path (which task-
            # cancels run_agent) is still the escape hatch.
            _raise_if_cancelled(cancel_event)

    if not saw_terminal_marker:
        raise RuntimeError(
            "upstream SSE stream ended before [DONE] / finish_reason — refusing "
            "to persist a possibly-truncated assistant turn"
        )

    text = "".join(text_parts)
    tool_calls = [tool_calls_by_idx[i] for i in sorted(tool_calls_by_idx.keys())]
    return text, tool_calls


def _accumulate_usage(
    sink: dict[str, Any] | None, usage: dict[str, Any] | None
) -> None:
    """Sum upstream token counts into `sink` across agent-loop iterations.

    OpenAI calls them prompt_tokens / completion_tokens; some providers
    use input_tokens / output_tokens. Accept either and normalise.
    """
    if sink is None or not isinstance(usage, dict):
        return
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if isinstance(prompt, int):
        sink["input_tokens"] = sink.get("input_tokens", 0) + prompt
    if isinstance(completion, int):
        sink["output_tokens"] = sink.get("output_tokens", 0) + completion


def _accumulate_tool_call(
    sink: dict[int, dict[str, Any]], delta_tc: dict[str, Any]
) -> None:
    idx = delta_tc.get("index", 0)
    target = sink.setdefault(idx, {"function": {}})
    if "id" in delta_tc:
        target["id"] = delta_tc["id"]
    if "type" in delta_tc:
        target["type"] = delta_tc["type"]
    fn_delta = delta_tc.get("function") or {}
    target_fn = target["function"]
    if "name" in fn_delta:
        target_fn["name"] = fn_delta["name"]
    if "arguments" in fn_delta:
        target_fn["arguments"] = target_fn.get("arguments", "") + fn_delta["arguments"]


def _history_row_to_request_message(m: Any) -> dict[str, Any]:
    if m.role == "tool" and m.meta_json:
        try:
            meta = json.loads(m.meta_json)
        except json.JSONDecodeError:
            return {"role": m.role, "content": m.content}
        return {
            "role": "tool",
            "tool_call_id": meta.get("tool_call_id", ""),
            "content": m.content,
        }
    if m.role == "assistant" and m.meta_json:
        try:
            meta = json.loads(m.meta_json)
        except json.JSONDecodeError:
            return {"role": m.role, "content": m.content}
        tool_calls = meta.get("tool_calls")
        if tool_calls:
            return {
                "role": "assistant",
                "content": m.content,
                "tool_calls": tool_calls,
            }
    return {"role": m.role, "content": m.content}


def _format_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_schema,
            },
        }
        for t in tools
    ]


def _parse_tool_arguments(call: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Decode a tool call's `arguments` into a dict. Returns `(args, error)`
    where `error` is a human message when the arguments are malformed (and
    `args` is then empty). Surfaced both to the on_tool_call callback / card
    and, on error, fed back to the LLM as the tool result."""
    raw = call["function"].get("arguments")
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"invalid arguments json ({exc})"
        if not isinstance(parsed, dict):
            return {}, f"invalid arguments shape ({type(parsed).__name__})"
        return parsed, None
    return {}, f"invalid arguments shape ({type(raw).__name__})"


def _denied_tool_result(reason: str | None) -> str:
    """The tool-result text fed back to the LLM when the user denies an
    approval. Mirrors the `error: ...` shape `_execute_tool_call` uses so the
    LLM treats it as a recoverable tool failure."""
    if reason:
        return f"error: denied by user: {reason}"
    return "error: denied by user"


async def _execute_tool_call(
    call: dict[str, Any],
    lookup: dict[str, Tool],
    args: dict[str, Any],
    arg_error: str | None,
) -> tuple[str, str]:
    """Run a tool and return `(status, content)`. `status` is "success" or
    "error"; `content` is the tool output (success) or an `error: ...` string
    (which is still fed back to the LLM so it can recover)."""
    name = call["function"]["name"]
    tool = lookup.get(name)
    if tool is None:
        return "error", f"error: unknown tool {name!r}"
    if arg_error is not None:
        return "error", f"error: {arg_error}"

    try:
        return "success", await tool.handler(args)
    except Exception as exc:  # noqa: BLE001 — surface to LLM, don't crash the loop
        return "error", f"error: {exc}"
