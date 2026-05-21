import json
from typing import Any

import aiosqlite
import httpx
import pytest

from hermes.agent import Tool, run_agent
from hermes.repository import conversations, messages

DEFAULT_SYSTEM = "You are Hermes."
MODEL = "claude-opus-4-7"


def _make_upstream(
    responses: list[dict[str, Any]],
) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    iter_responses = iter(responses)
    requests_seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(json.loads(request.content))
        try:
            payload = next(iter_responses)
        except StopIteration as exc:
            raise AssertionError("upstream called more times than expected") from exc
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )
    return client, requests_seen


def _assistant_response(
    content: str = "", *, tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    return {
        "id": "chatcmpl-test",
        "model": MODEL,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
    }


async def _noop_async(_: dict[str, Any]) -> str:
    return "ok"


async def test_run_agent_returns_text_and_persists_assistant_message(
    conn: aiosqlite.Connection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await messages.append(conn, conversation_id=convo.id, role="user", content="hi", ts=1001)

    upstream, _ = _make_upstream([_assistant_response("hello back")])

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
    )

    assert text == "hello back"
    msgs = await messages.list_by_conversation(conn, convo.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[-1].content == "hello back"


async def test_run_agent_injects_system_prompt_and_history(
    conn: aiosqlite.Connection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await messages.append(conn, conversation_id=convo.id, role="user", content="first", ts=1001)
    await messages.append(
        conn, conversation_id=convo.id, role="assistant", content="reply 1", ts=1002
    )
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="follow-up", ts=1003
    )

    upstream, requests_seen = _make_upstream([_assistant_response("ok")])

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
    )

    sent = requests_seen[0]
    assert sent["model"] == MODEL
    assert sent["messages"][0] == {"role": "system", "content": DEFAULT_SYSTEM}
    assert sent["messages"][1:] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "follow-up"},
    ]


async def test_run_agent_includes_tools_definition_in_request(
    conn: aiosqlite.Connection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await messages.append(conn, conversation_id=convo.id, role="user", content="hi", ts=1001)

    upstream, requests_seen = _make_upstream([_assistant_response("hi back")])

    tool = Tool(
        name="foo",
        description="does foo",
        parameters_schema={"type": "object", "properties": {}},
        handler=_noop_async,
    )

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
        tools=[tool],
    )

    sent = requests_seen[0]
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "foo",
                "description": "does foo",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


async def test_run_agent_executes_tool_calls_and_loops(
    conn: aiosqlite.Connection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="search standup", ts=1001
    )

    tool_call = {
        "id": "call_xyz",
        "type": "function",
        "function": {
            "name": "echo",
            "arguments": json.dumps({"text": "standup notes"}),
        },
    }
    upstream, requests_seen = _make_upstream(
        [
            _assistant_response("", tool_calls=[tool_call]),
            _assistant_response("Found: standup notes"),
        ]
    )

    echo_calls: list[dict[str, Any]] = []

    async def echo_handler(args: dict[str, Any]) -> str:
        echo_calls.append(args)
        return f"echoed: {args['text']}"

    echo_tool = Tool(
        name="echo",
        description="Echo back the text",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=echo_handler,
    )

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
        tools=[echo_tool],
    )

    assert text == "Found: standup notes"
    assert echo_calls == [{"text": "standup notes"}]

    # Second request includes the tool result message.
    second = requests_seen[1]
    tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_xyz"
    assert tool_msgs[0]["content"] == "echoed: standup notes"

    # All turns persisted: user + assistant(tool_calls) + tool + assistant(final).
    msgs = await messages.list_by_conversation(conn, convo.id)
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[-1].content == "Found: standup notes"


async def test_run_agent_raises_when_max_iterations_exceeded(
    conn: aiosqlite.Connection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="loop forever", ts=1001
    )

    tool_call = {
        "id": "call_x",
        "type": "function",
        "function": {"name": "echo", "arguments": "{}"},
    }
    upstream, _ = _make_upstream([_assistant_response("", tool_calls=[tool_call])] * 3)

    async def echo_handler(_: dict[str, Any]) -> str:
        return "x"

    tool = Tool(
        name="echo",
        description="echo",
        parameters_schema={"type": "object", "properties": {}},
        handler=echo_handler,
    )

    with pytest.raises(RuntimeError, match="max_iterations"):
        await run_agent(
            upstream=upstream,
            db=conn,
            conversation_id=convo.id,
            system_prompt=DEFAULT_SYSTEM,
            model=MODEL,
            tools=[tool],
            max_iterations=2,
        )
