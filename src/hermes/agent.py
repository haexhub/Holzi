import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiosqlite
import httpx

from hermes.repository import messages

CHAT_PATH = "/v1/chat/completions"


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
) -> str:
    """Run a single agent turn until the assistant stops requesting tool calls.

    History is loaded from the DB (the caller is expected to have persisted
    the new user message already). Each LLM call is one iteration; tool
    results are persisted and fed back as the next request. Returns the
    final assistant text.
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

        resp = await upstream.post(CHAT_PATH, json=body)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            text = str(msg.get("content") or "")
            await messages.append(
                db, conversation_id=conversation_id, role="assistant", content=text
            )
            return text

        # Assistant turn that requested tools — persist it with the tool_calls
        # in meta_json so we can replay it later if needed.
        assistant_content = str(msg.get("content") or "")
        request_messages.append(
            {
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": tool_calls,
            }
        )
        await messages.append(
            db,
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_content,
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
        # Some providers send arguments as a JSON object directly instead of
        # the OpenAI default of a JSON-encoded string.
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
