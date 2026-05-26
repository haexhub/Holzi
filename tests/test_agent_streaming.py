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
    body = _sse_chunks([{"content": c} for c in text_chunks])

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
