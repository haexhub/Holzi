import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite
import httpx

from hermes.repository import messages

CHAT_PATH = "/v1/chat/completions"

OnChunk = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]


async def run_agent(
    *,
    upstream: httpx.AsyncClient,
    db: aiosqlite.Connection,
    conversation_id: int,
    system_prompt: str,
    model: str,
    tools: list[Tool] | None = None,
    max_iterations: int = 10,
    on_chunk: OnChunk | None = None,
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
    """
    history = await messages.list_by_conversation(db, conversation_id)
    request_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for m in history:
        request_messages.append(_history_row_to_request_message(m))

    tools_payload = _format_tools(tools) if tools else None
    tool_lookup = {t.name: t for t in tools} if tools else {}

    for _ in range(max_iterations):
        body: dict[str, Any] = {"model": model, "messages": request_messages}
        if tools_payload:
            body["tools"] = tools_payload

        if on_chunk is None:
            assistant_text, tool_calls = await _request_round_nonstream(upstream, body)
        else:
            body["stream"] = True
            assistant_text, tool_calls = await _request_round_stream(
                upstream, body, on_chunk
            )

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
            result = await _execute_tool_call(call, tool_lookup)
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
                    {"tool_call_id": call["id"], "name": call["function"]["name"]}
                ),
            )

    raise RuntimeError(f"agent loop exceeded max_iterations={max_iterations}")


async def _request_round_nonstream(
    upstream: httpx.AsyncClient, body: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    resp = await upstream.post(CHAT_PATH, json=body)
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]
    text = str(msg.get("content") or "")
    tool_calls = list(msg.get("tool_calls") or [])
    return text, tool_calls


async def _request_round_stream(
    upstream: httpx.AsyncClient,
    body: dict[str, Any],
    on_chunk: OnChunk,
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

    if not saw_terminal_marker:
        raise RuntimeError(
            "upstream SSE stream ended before [DONE] / finish_reason — refusing "
            "to persist a possibly-truncated assistant turn"
        )

    text = "".join(text_parts)
    tool_calls = [tool_calls_by_idx[i] for i in sorted(tool_calls_by_idx.keys())]
    return text, tool_calls


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


async def _execute_tool_call(call: dict[str, Any], lookup: dict[str, Tool]) -> str:
    name = call["function"]["name"]
    tool = lookup.get(name)
    if tool is None:
        return f"error: unknown tool {name!r}"

    raw = call["function"].get("arguments")
    if raw is None or raw == "":
        args: dict[str, Any] = {}
    elif isinstance(raw, dict):
        args = raw
    elif isinstance(raw, str):
        try:
            args = json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"error: invalid arguments json ({exc})"
    else:
        return f"error: invalid arguments shape ({type(raw).__name__})"

    try:
        return await tool.handler(args)
    except Exception as exc:  # noqa: BLE001 — surface to LLM, don't crash the loop
        return f"error: {exc}"
