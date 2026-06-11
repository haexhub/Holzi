import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import ApprovalDecision, Tool, run_agent
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
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
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
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
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
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
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
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
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
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
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


async def test_run_agent_accepts_object_tool_arguments(
    conn: AsyncEngine,
) -> None:
    """Some providers send `function.arguments` as a dict instead of a JSON string."""
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="echo please", ts=1001
    )

    tool_call = {
        "id": "call_obj",
        "type": "function",
        "function": {"name": "echo", "arguments": {"text": "hello"}},
    }
    upstream, _ = _make_upstream(
        [
            _assistant_response("", tool_calls=[tool_call]),
            _assistant_response("done"),
        ]
    )

    seen: list[dict[str, Any]] = []

    async def echo_handler(args: dict[str, Any]) -> str:
        seen.append(args)
        return "ok"

    tool = Tool(
        name="echo",
        description="echo",
        parameters_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=echo_handler,
    )

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
        tools=[tool],
    )
    assert text == "done"
    assert seen == [{"text": "hello"}]


def _risky_tool(handler: Any, *, reason: str = "does something risky") -> Tool:
    return Tool(
        name="risky",
        description="a risky tool",
        parameters_schema={"type": "object", "properties": {}},
        handler=handler,
        requires_approval=True,
        risk_reason=reason,
    )


async def test_run_agent_gates_risky_tool_and_executes_on_allow(
    conn: AsyncEngine,
) -> None:
    """A requires_approval tool calls on_approval first; on allow_once it runs
    normally and emits the usual tool_call/tool_result callbacks."""
    convo = await conversations.create(conn, user_id=1, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="do it", ts=1001
    )

    tool_call = {
        "id": "call_risky",
        "type": "function",
        "function": {"name": "risky", "arguments": json.dumps({"x": 1})},
    }
    upstream, _ = _make_upstream(
        [
            _assistant_response("", tool_calls=[tool_call]),
            _assistant_response("all done"),
        ]
    )

    ran: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        ran.append(args)
        return "executed"

    approval_calls: list[tuple[str, str, dict[str, Any], str]] = []

    async def on_approval(
        call_id: str, name: str, args: dict[str, Any], reason: str
    ) -> ApprovalDecision:
        approval_calls.append((call_id, name, args, reason))
        return ApprovalDecision(decision="allow_once")

    tool_calls_seen: list[str] = []
    tool_results_seen: list[tuple[str, str]] = []

    async def on_tool_call(call_id: str, _name: str, _args: dict[str, Any]) -> None:
        tool_calls_seen.append(call_id)

    async def on_tool_result(call_id: str, status: str, _content: str) -> None:
        tool_results_seen.append((call_id, status))

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
        tools=[_risky_tool(handler)],
        on_approval=on_approval,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )

    assert text == "all done"
    # The tool actually ran exactly once.
    assert ran == [{"x": 1}]
    # on_approval was consulted with the call id, name, args and risk reason.
    assert approval_calls == [("call_risky", "risky", {"x": 1}, "does something risky")]
    # Allow path still surfaces the normal tool_call + success result.
    assert tool_calls_seen == ["call_risky"]
    assert tool_results_seen == [("call_risky", "success")]


async def test_run_agent_denies_risky_tool_without_executing(
    conn: AsyncEngine,
) -> None:
    """On deny the tool never runs; a denied result is fed back to the LLM and
    persisted as an error tool turn. No tool_call callback fires (nothing ran)."""
    convo = await conversations.create(conn, user_id=1, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="do it", ts=1001
    )

    tool_call = {
        "id": "call_risky",
        "type": "function",
        "function": {"name": "risky", "arguments": "{}"},
    }
    upstream, requests_seen = _make_upstream(
        [
            _assistant_response("", tool_calls=[tool_call]),
            _assistant_response("ok, skipped"),
        ]
    )

    ran = False

    async def handler(_: dict[str, Any]) -> str:
        nonlocal ran
        ran = True
        return "executed"

    async def on_approval(*_: Any) -> ApprovalDecision:
        return ApprovalDecision(decision="deny", reason="not now")

    tool_calls_seen: list[str] = []

    async def on_tool_call(call_id: str, _name: str, _args: dict[str, Any]) -> None:
        tool_calls_seen.append(call_id)

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
        tools=[_risky_tool(handler)],
        on_approval=on_approval,
        on_tool_call=on_tool_call,
    )

    assert text == "ok, skipped"
    assert ran is False
    # No tool_call callback for a denied (never-executed) action.
    assert tool_calls_seen == []

    # The LLM saw a denied tool result mentioning the reason in the 2nd round.
    second = requests_seen[1]
    tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "deni" in tool_msgs[0]["content"].lower()
    assert "not now" in tool_msgs[0]["content"]

    # Persisted as an error tool turn.
    msgs = await messages.list_by_conversation(conn, convo.id)
    tool_rows = [m for m in msgs if m.role == "tool"]
    assert len(tool_rows) == 1
    meta = json.loads(tool_rows[0].meta_json)
    assert meta["status"] == "error"


async def test_run_agent_ignores_approval_flag_without_callback(
    conn: AsyncEngine,
) -> None:
    """Channels without an approval UI (Signal/MCP pass no on_approval) execute
    a requires_approval tool normally — the gate only engages when a callback
    is supplied."""
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="do it", ts=1001
    )

    tool_call = {
        "id": "call_risky",
        "type": "function",
        "function": {"name": "risky", "arguments": "{}"},
    }
    upstream, _ = _make_upstream(
        [
            _assistant_response("", tool_calls=[tool_call]),
            _assistant_response("done"),
        ]
    )

    ran = False

    async def handler(_: dict[str, Any]) -> str:
        nonlocal ran
        ran = True
        return "executed"

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=DEFAULT_SYSTEM,
        model=MODEL,
        tools=[_risky_tool(handler)],
    )

    assert text == "done"
    assert ran is True
