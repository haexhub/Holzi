import json
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool, run_agent
from hermes.repository import conversations, messages

MODEL = "claude-opus-4-7"
SYSTEM = "You are Hermes."


def _sse_chunks(deltas: list[dict[str, Any]]) -> bytes:
    """Serialise a list of OpenAI-style streaming chunks to an SSE byte body."""
    out: list[bytes] = []
    for d in deltas:
        chunk = {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "model": MODEL,
            "choices": [{"index": 0, "delta": d, "finish_reason": None}],
        }
        out.append(f"data: {json.dumps(chunk)}\n\n".encode())
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


def _streaming_text_handler(text_chunks: list[str]):
    return _streaming_handler([{"content": c} for c in text_chunks])


def _streaming_handler(deltas: list[dict[str, Any]]):
    """Stream arbitrary OpenAI-style deltas (content, reasoning_content, …)."""
    body = _sse_chunks(deltas)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    return handler


def _two_round_handler(
    tool_call_deltas: list[dict[str, Any]],
    final_text_chunks: list[str],
) -> tuple[Any, list[dict[str, Any]]]:
    """Round 1 streams a tool_call; round 2 streams plain text."""
    seen: list[dict[str, Any]] = []
    round_bodies = [
        _sse_chunks(tool_call_deltas),
        _sse_chunks([{"content": c} for c in final_text_chunks]),
    ]
    it = iter(round_bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(next(it)),
        )

    return handler, seen


def _mock_upstream(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )


async def test_run_agent_with_on_chunk_streams_each_text_delta(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    upstream = _mock_upstream(_streaming_text_handler(["Hello", " ", "world"]))

    chunks_seen: list[str] = []

    async def on_chunk(c: str) -> None:
        chunks_seen.append(c)

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        on_chunk=on_chunk,
    )

    assert text == "Hello world"
    assert chunks_seen == ["Hello", " ", "world"]
    # Persisted final text once, not per chunk.
    msgs = await messages.list_by_conversation(conn, convo.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[-1].content == "Hello world"


async def test_run_agent_with_on_chunk_sets_stream_true_on_upstream(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(_sse_chunks([{"content": "x"}])),
        )

    upstream = _mock_upstream(handler)

    async def on_chunk(_: str) -> None:
        return None

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        on_chunk=on_chunk,
    )
    assert captured[0]["stream"] is True
    # No metrics dict requested → don't ask the provider for usage.
    assert "stream_options" not in captured[0]


async def test_run_agent_streaming_requests_usage_when_metrics_provided(
    conn: AsyncEngine,
) -> None:
    """When the caller passes a `metrics` dict, the upstream streaming
    request must opt into usage reporting via stream_options — otherwise
    OpenAI-compatible providers omit the terminal usage chunk."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(_sse_chunks([{"content": "x"}])),
        )

    upstream = _mock_upstream(handler)

    async def on_chunk(_: str) -> None:
        return None

    metrics: dict[str, Any] = {}
    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        on_chunk=on_chunk,
        metrics=metrics,
    )
    assert captured[0]["stream_options"] == {"include_usage": True}


async def test_run_agent_streaming_handles_tool_call_round(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="echo me", ts=1001
    )

    # Tool-call streamed across multiple deltas — id+name on the first delta,
    # arguments accumulated across two deltas.
    tool_call_deltas: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text":'},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"arguments": '"hi"}'},
                }
            ]
        },
    ]
    final_chunks = ["Final ", "answer."]
    handler, _ = _two_round_handler(tool_call_deltas, final_chunks)
    upstream = _mock_upstream(handler)

    chunks_seen: list[str] = []

    async def on_chunk(c: str) -> None:
        chunks_seen.append(c)

    echo_args_seen: list[dict[str, Any]] = []

    async def echo_handler(args: dict[str, Any]) -> str:
        echo_args_seen.append(args)
        return f"echoed: {args['text']}"

    tool = Tool(
        name="echo",
        description="echo it",
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
        system_prompt=SYSTEM,
        model=MODEL,
        tools=[tool],
        on_chunk=on_chunk,
    )

    assert text == "Final answer."
    # Tool call assembled correctly across deltas.
    assert echo_args_seen == [{"text": "hi"}]
    # Only the final-text round produced chunks visible to the caller.
    assert chunks_seen == ["Final ", "answer."]


async def test_run_agent_streaming_raises_on_truncated_stream(
    conn: AsyncEngine,
) -> None:
    """If the upstream connection drops before [DONE], the partial text must
    not be silently persisted as a completed assistant turn."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    # Two text deltas, then EOF without `[DONE]` and no finish_reason.
    truncated_body = (
        b'data: {"choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(truncated_body),
        )

    upstream = _mock_upstream(handler)

    async def on_chunk(_: str) -> None:
        return None

    import pytest

    with pytest.raises(RuntimeError, match="stream ended"):
        await run_agent(
            upstream=upstream,
            db=conn,
            conversation_id=convo.id,
            system_prompt=SYSTEM,
            model=MODEL,
            on_chunk=on_chunk,
        )

    # Nothing should have been persisted as a completed assistant turn.
    msgs = await messages.list_by_conversation(conn, convo.id)
    assert [m.role for m in msgs] == ["user"]


async def test_run_agent_streaming_accepts_finish_reason_as_terminal(
    conn: AsyncEngine,
) -> None:
    """Some providers don't emit `[DONE]` but do set finish_reason on the
    last chunk — that should also count as a clean completion."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    body = (
        b'data: {"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    upstream = _mock_upstream(handler)

    async def on_chunk(_: str) -> None:
        return None

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        on_chunk=on_chunk,
    )
    assert text == "hello"


async def test_run_agent_raises_cancelled_if_event_set_before_upstream(
    conn: AsyncEngine,
) -> None:
    """If the cancel_event is set before run_agent's first upstream call,
    the agent must raise ChatRunCancelled without touching upstream and
    without persisting an assistant turn."""
    import asyncio

    from hermes.agent import ChatRunCancelled

    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    upstream_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(_sse_chunks([{"content": "should not stream"}])),
        )

    upstream = _mock_upstream(handler)

    cancel_event = asyncio.Event()
    cancel_event.set()

    async def on_chunk(_: str) -> None:
        return None

    import pytest

    with pytest.raises(ChatRunCancelled):
        await run_agent(
            upstream=upstream,
            db=conn,
            conversation_id=convo.id,
            system_prompt=SYSTEM,
            model=MODEL,
            on_chunk=on_chunk,
            cancel_event=cancel_event,
        )

    assert upstream_calls == []
    msgs = await messages.list_by_conversation(conn, convo.id)
    assert [m.role for m in msgs] == ["user"]


async def test_run_agent_raises_cancelled_after_stream_chunk(
    conn: AsyncEngine,
) -> None:
    """When the cancel_event is set mid-stream (between deltas), run_agent
    must abort cleanly at the next chunk boundary and raise
    ChatRunCancelled without persisting the partial answer."""
    import asyncio

    from hermes.agent import ChatRunCancelled

    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    upstream = _mock_upstream(_streaming_text_handler(["one", "two", "three"]))

    cancel_event = asyncio.Event()
    chunks_seen: list[str] = []

    async def on_chunk(c: str) -> None:
        chunks_seen.append(c)
        # Set cancel right after the first delta — the agent should
        # check the event before forwarding the next chunk.
        cancel_event.set()

    import pytest

    with pytest.raises(ChatRunCancelled):
        await run_agent(
            upstream=upstream,
            db=conn,
            conversation_id=convo.id,
            system_prompt=SYSTEM,
            model=MODEL,
            on_chunk=on_chunk,
            cancel_event=cancel_event,
        )

    # No fake "completed" assistant row.
    msgs = await messages.list_by_conversation(conn, convo.id)
    assert [m.role for m in msgs] == ["user"]


async def test_run_agent_raises_cancelled_before_tool_execution(
    conn: AsyncEngine,
) -> None:
    """If the cancel_event is set after a tool_call is received but before
    we execute the tool, the tool must not run and ChatRunCancelled is
    raised."""
    import asyncio

    from hermes.agent import ChatRunCancelled

    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    tool_call_deltas: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text":"hi"}'},
                }
            ]
        },
    ]
    body = _sse_chunks(tool_call_deltas)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    upstream = _mock_upstream(handler)

    cancel_event = asyncio.Event()
    tool_calls_executed: list[dict[str, Any]] = []

    async def echo_handler(args: dict[str, Any]) -> str:
        tool_calls_executed.append(args)
        return "executed"

    tool = Tool(
        name="echo",
        description="echo it",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
        handler=echo_handler,
    )

    # Pre-arm cancel so as soon as the assistant turn finishes streaming
    # and we enter the tool-execution phase, the agent aborts.
    cancel_event.set()

    async def on_chunk(_: str) -> None:
        return None

    import pytest

    with pytest.raises(ChatRunCancelled):
        await run_agent(
            upstream=upstream,
            db=conn,
            conversation_id=convo.id,
            system_prompt=SYSTEM,
            model=MODEL,
            tools=[tool],
            on_chunk=on_chunk,
            cancel_event=cancel_event,
        )

    assert tool_calls_executed == []


async def test_run_agent_without_on_chunk_stays_non_streaming(
    conn: AsyncEngine,
) -> None:
    """Backwards-compat: callers without on_chunk (Signal worker, MCP, tests)
    keep getting the JSON-response path."""
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    upstream = _mock_upstream(handler)
    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
    )
    assert text == "ok"
    assert "stream" not in captured[0]


async def test_run_agent_streaming_emits_tool_callbacks_and_persists_metadata(
    conn: AsyncEngine,
) -> None:
    """A tool round fires on_tool_call before execution and on_tool_result
    after, and persists name/arguments/status in the tool message's meta_json
    so the conversation can be reconstructed on reload."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="echo me", ts=1001
    )

    tool_call_deltas: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text":"hi"}'},
                }
            ]
        },
    ]
    handler, _ = _two_round_handler(tool_call_deltas, ["done"])
    upstream = _mock_upstream(handler)

    async def echo_handler(args: dict[str, Any]) -> str:
        return f"echoed: {args['text']}"

    tool = Tool(
        name="echo",
        description="echo it",
        parameters_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=echo_handler,
    )

    tool_calls_seen: list[tuple[str, str, dict[str, Any]]] = []
    tool_results_seen: list[tuple[str, str, str]] = []

    async def on_tool_call(call_id: str, name: str, args: dict[str, Any]) -> None:
        tool_calls_seen.append((call_id, name, args))

    async def on_tool_result(call_id: str, status: str, content: str) -> None:
        tool_results_seen.append((call_id, status, content))

    async def on_chunk(_: str) -> None:
        return None

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        tools=[tool],
        on_chunk=on_chunk,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )

    assert tool_calls_seen == [("call_abc", "echo", {"text": "hi"})]
    assert tool_results_seen == [("call_abc", "success", "echoed: hi")]

    msgs = await messages.list_by_conversation(conn, convo.id)
    tool_msg = next(m for m in msgs if m.role == "tool")
    meta = json.loads(tool_msg.meta_json)
    assert meta["tool_call_id"] == "call_abc"
    assert meta["name"] == "echo"
    assert meta["arguments"] == {"text": "hi"}
    assert meta["status"] == "success"
    assert tool_msg.content == "echoed: hi"


async def test_run_agent_streaming_marks_tool_error_status(
    conn: AsyncEngine,
) -> None:
    """A tool whose handler raises is reported with status=error to the
    callback and persisted as status=error."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="boom", ts=1001
    )

    tool_call_deltas: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_err",
                    "type": "function",
                    "function": {"name": "boom", "arguments": "{}"},
                }
            ]
        },
    ]
    handler, _ = _two_round_handler(tool_call_deltas, ["recovered"])
    upstream = _mock_upstream(handler)

    async def boom_handler(_args: dict[str, Any]) -> str:
        raise ValueError("kaboom")

    tool = Tool(
        name="boom",
        description="explodes",
        parameters_schema={"type": "object", "properties": {}},
        handler=boom_handler,
    )

    tool_results_seen: list[tuple[str, str, str]] = []

    async def on_tool_result(call_id: str, status: str, content: str) -> None:
        tool_results_seen.append((call_id, status, content))

    async def on_chunk(_: str) -> None:
        return None

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        tools=[tool],
        on_chunk=on_chunk,
        on_tool_result=on_tool_result,
    )

    assert len(tool_results_seen) == 1
    call_id, status, content = tool_results_seen[0]
    assert call_id == "call_err"
    assert status == "error"
    assert "kaboom" in content

    msgs = await messages.list_by_conversation(conn, convo.id)
    tool_msg = next(m for m in msgs if m.role == "tool")
    meta = json.loads(tool_msg.meta_json)
    assert meta["status"] == "error"


async def test_run_agent_streaming_forwards_and_persists_reasoning(
    conn: AsyncEngine,
) -> None:
    """Reasoning deltas (`delta.reasoning_content`) are forwarded to the
    on_reasoning callback as they arrive and persisted on the assistant
    message's meta_json so the card can re-render on reload."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    upstream = _mock_upstream(
        _streaming_handler(
            [
                {"reasoning_content": "Let me "},
                {"reasoning_content": "think."},
                {"content": "Hello"},
                {"content": " world"},
            ]
        )
    )

    reasoning_seen: list[str] = []
    text_seen: list[str] = []

    async def on_reasoning(c: str) -> None:
        reasoning_seen.append(c)

    async def on_chunk(c: str) -> None:
        text_seen.append(c)

    text = await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        on_chunk=on_chunk,
        on_reasoning=on_reasoning,
    )

    assert text == "Hello world"
    assert reasoning_seen == ["Let me ", "think."]
    # Reasoning is not mixed into the visible answer.
    assert text_seen == ["Hello", " world"]

    msgs = await messages.list_by_conversation(conn, convo.id)
    assistant = next(m for m in msgs if m.role == "assistant")
    assert assistant.content == "Hello world"
    meta = json.loads(assistant.meta_json)
    assert meta["reasoning"] == "Let me think."


async def test_run_agent_streaming_reasoning_field_fallback(
    conn: AsyncEngine,
) -> None:
    """Some providers name the field `reasoning` rather than
    `reasoning_content`; both are accepted."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    upstream = _mock_upstream(
        _streaming_handler([{"reasoning": "hmm"}, {"content": "ok"}])
    )

    reasoning_seen: list[str] = []

    async def on_reasoning(c: str) -> None:
        reasoning_seen.append(c)

    async def on_chunk(_: str) -> None:
        return None

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        on_chunk=on_chunk,
        on_reasoning=on_reasoning,
    )
    assert reasoning_seen == ["hmm"]


async def test_run_agent_streaming_without_reasoning_leaves_meta_null(
    conn: AsyncEngine,
) -> None:
    """A provider that emits no reasoning must leave the assistant turn
    exactly as before — no meta_json, no callback fired."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="hi", ts=1001
    )

    upstream = _mock_upstream(_streaming_text_handler(["plain", " answer"]))

    reasoning_seen: list[str] = []

    async def on_reasoning(c: str) -> None:
        reasoning_seen.append(c)

    async def on_chunk(_: str) -> None:
        return None

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        on_chunk=on_chunk,
        on_reasoning=on_reasoning,
    )

    assert reasoning_seen == []
    msgs = await messages.list_by_conversation(conn, convo.id)
    assistant = next(m for m in msgs if m.role == "assistant")
    assert assistant.meta_json is None


async def test_run_agent_streaming_rejects_non_object_tool_arguments(
    conn: AsyncEngine,
) -> None:
    """Valid JSON that isn't an object (e.g. a bare number) must produce a
    clean tool error, not flow a non-dict into the handler/persistence."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="go", ts=1001
    )

    tool_call_deltas: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "echo", "arguments": "123"},
                }
            ]
        },
    ]
    handler, _ = _two_round_handler(tool_call_deltas, ["recovered"])
    upstream = _mock_upstream(handler)

    called = False

    async def echo_handler(args: dict[str, Any]) -> str:
        nonlocal called
        called = True
        return "ok"

    tool = Tool(
        name="echo",
        description="echo",
        parameters_schema={"type": "object", "properties": {}},
        handler=echo_handler,
    )

    results: list[tuple[str, str, str]] = []

    async def on_tool_result(call_id: str, status: str, content: str) -> None:
        results.append((call_id, status, content))

    async def on_chunk(_: str) -> None:
        return None

    await run_agent(
        upstream=upstream,
        db=conn,
        conversation_id=convo.id,
        system_prompt=SYSTEM,
        model=MODEL,
        tools=[tool],
        on_chunk=on_chunk,
        on_tool_result=on_tool_result,
    )

    # Handler never ran; the malformed arguments became a clean error result.
    assert called is False
    assert len(results) == 1
    assert results[0][1] == "error"
    assert "shape" in results[0][2]

    msgs = await messages.list_by_conversation(conn, convo.id)
    tool_msg = next(m for m in msgs if m.role == "tool")
    meta = json.loads(tool_msg.meta_json)
    assert meta["status"] == "error"
    assert meta["arguments"] == {}
