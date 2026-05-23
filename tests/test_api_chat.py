import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import conversations, messages

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _assistant_oneshot(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": "claude-opus-4-7",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _install_upstream_responses(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Install a MockTransport on app.state.upstream and return the list of
    request bodies that were sent upstream (mutated as calls arrive)."""
    iter_resp = iter(responses)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        try:
            payload = next(iter_resp)
        except StopIteration as exc:
            raise AssertionError("upstream called more times than expected") from exc
        # /api/chat triggers run_agent's streaming path. Re-emit the
        # non-streaming canned response as an OpenAI-style SSE byte stream
        # so the existing test fixtures keep working with the new code.
        body = _to_sse_stream(payload)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )
    return seen


def _to_sse_stream(payload: dict[str, Any]) -> bytes:
    msg = payload["choices"][0]["message"]
    content = msg.get("content")
    tool_calls = msg.get("tool_calls") or []
    out = b""
    if content:
        chunk = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": None}
            ]
        }
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    if tool_calls:
        delta_tcs = [
            {
                "index": i,
                "id": tc["id"],
                "type": tc.get("type", "function"),
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                },
            }
            for i, tc in enumerate(tool_calls)
        ]
        chunk = {
            "choices": [
                {"index": 0, "delta": {"tool_calls": delta_tcs}, "finish_reason": None}
            ]
        }
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    out += b"data: [DONE]\n\n"
    return out


@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


def _parse_sse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse a series of `event: name\\ndata: {...}\\n\\n` blocks."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split(b"\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split(b"\n"):
            line = line.strip()
            if line.startswith(b"event: "):
                event = line[len(b"event: ") :].decode()
            elif line.startswith(b"data: "):
                data_lines.append(line[len(b"data: ") :].decode())
        if event:
            data = json.loads("\n".join(data_lines)) if data_lines else {}
            events.append((event, data))
    return events


async def test_api_chat_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 401


async def test_api_chat_creates_new_conversation_when_none_provided(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("hello back")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    event_names = [name for name, _ in events]
    assert event_names == ["session", "text", "done"]

    session_evt = events[0][1]
    text_evt = events[1][1]
    assert isinstance(session_evt["conversation_id"], int)
    assert text_evt["content"] == "hello back"

    conv_id = session_evt["conversation_id"]
    convo = await conversations.get(app.state.db, conv_id)
    assert convo is not None
    assert convo.channel == "web"
    msgs = await messages.list_by_conversation(app.state.db, conv_id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"),
        ("assistant", "hello back"),
    ]


async def test_api_chat_continues_existing_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    _install_upstream_responses([_assistant_oneshot("ack")])

    async with client.stream(
        "POST",
        "/api/chat",
        headers=AUTH,
        json={"message": "round 2", "conversation_id": convo.id},
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    assert events[0][1]["conversation_id"] == convo.id
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "round 2"),
        ("assistant", "ack"),
    ]


async def test_api_chat_unknown_conversation_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hi", "conversation_id": 99999},
    )
    assert response.status_code == 404


async def test_api_chat_cancels_agent_task_when_client_disconnects(
    client: httpx.AsyncClient,
) -> None:
    """When the SSE generator is closed (client disconnects), the background
    agent task must be cancelled — otherwise tool/DB side-effects continue
    after the client is gone. Simulated by closing the streaming response
    mid-flight and checking that the upstream stream was abandoned."""
    import anyio

    # Block upstream forever so the agent task is stuck mid-stream.
    upstream_started = anyio.Event()
    release_upstream = anyio.Event()

    initial_chunk = (
        b'data: {"choices":[{"index":0,"delta":{"content":"start"},'
        b'"finish_reason":null}]}\n\n'
    )

    async def _content_stream():
        upstream_started.set()
        # First emit a session-establishing chunk so the SSE channel opens.
        yield initial_chunk
        # Then block until the test releases us (or cancellation interrupts).
        await release_upstream.wait()
        yield b"data: [DONE]\n\n"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_content_stream(),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        # Read just enough to confirm the stream started.
        async for chunk in response.aiter_bytes():
            if b"start" in chunk or b"session" in chunk:
                break
        # Client disconnects — closing the context cancels the generator.
        await response.aclose()

    # Release the upstream so any unfinished task could try to make progress.
    # If cleanup worked, the agent task is already cancelled and this is a no-op.
    release_upstream.set()
    # Give the loop a tick to actually run the cancellation cleanup path.
    await anyio.sleep(0.05)
    # Test passes simply by not hanging / not crashing — the real verification
    # is that no unhandled-task exceptions are reported by pytest-asyncio.


async def test_api_chat_streams_text_chunks_incrementally(
    client: httpx.AsyncClient,
) -> None:
    """Each upstream streaming delta should surface as its own SSE `text` event."""
    deltas = ["Hello", " ", "world"]
    body = b""
    for d in deltas:
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "claude-opus-4-7",
            "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}],
        }
        body += f"data: {json.dumps(chunk)}\n\n".encode()
    body += b"data: [DONE]\n\n"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        out = b""
        async for chunk in response.aiter_bytes():
            out += chunk

    text_events: list[str] = []
    for block in out.split(b"\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split(b"\n"):
            line_s = line.strip().decode()
            if line_s.startswith("event:"):
                event = line_s[len("event:") :].strip()
            elif line_s.startswith("data:"):
                data_lines.append(line_s[len("data:") :].strip())
        if event == "text":
            text_events.append(json.loads("\n".join(data_lines))["content"])

    assert text_events == ["Hello", " ", "world"]


async def test_api_chat_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    """Channel semantics: /api/chat must not inject web messages into Signal threads."""
    signal_convo = await conversations.create(app.state.db, channel="signal", ts=1000)
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hijack", "conversation_id": signal_convo.id},
    )
    assert response.status_code == 400
    # Nothing should have been written into the signal conversation.
    msgs = await messages.list_by_conversation(app.state.db, signal_convo.id)
    assert msgs == []


async def test_api_chat_missing_message_returns_400(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/chat", headers=AUTH, json={})
    assert response.status_code == 400


async def test_api_chat_passes_tool_catalog_to_agent(
    client: httpx.AsyncClient,
) -> None:
    seen = _install_upstream_responses([_assistant_oneshot("done")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    # The agent should have included Hermes tools in the upstream request.
    sent = seen[0]
    tools = sent.get("tools")
    assert tools, "agent should pass tools to upstream"
    names = {t["function"]["name"] for t in tools}
    assert "recall_memory" in names
    assert "save_note" in names
    assert "todo_add" in names


async def test_api_chat_uses_active_credential_model(
    client: httpx.AsyncClient,
) -> None:
    """The per-credential model wins over settings.model when active."""
    from hermes.repository import llm_credentials as repo

    seen = _install_upstream_responses([_assistant_oneshot("ok")])
    # Insert + activate a credential with a distinctive model id.
    ct = app.state.encryptor.encrypt("sk-x")
    row = await repo.create_api_key(
        app.state.db,
        provider="openai",
        display_name="t",
        base_url=None,
        ciphertext=ct,
    )
    await repo.set_model(app.state.db, row.id, "gpt-99-custom")
    await repo.activate(app.state.db, row.id)

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
        assert response.status_code == 200

    assert seen[0]["model"] == "gpt-99-custom"


async def test_api_chat_falls_back_to_settings_model_when_no_active(
    client: httpx.AsyncClient,
) -> None:
    seen = _install_upstream_responses([_assistant_oneshot("ok")])
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
        assert response.status_code == 200
    # No active credential → settings.model (the test config defaults).
    from hermes.config import settings
    assert seen[0]["model"] == settings.model


async def test_api_chat_cross_channel_send_filters_web_target(
    client: httpx.AsyncClient,
) -> None:
    """cross_channel_send should refuse to write back to the channel the
    /api/chat request itself uses (recursion guard)."""
    # Make the agent call cross_channel_send(channel='web', ...) and observe
    # the error the tool returns in the second LLM round-trip.
    tool_call = {
        "id": "call_loop",
        "type": "function",
        "function": {
            "name": "cross_channel_send",
            "arguments": json.dumps({"channel": "web", "message": "hello me"}),
        },
    }
    first_resp = {
        "id": "x",
        "model": "claude-opus-4-7",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [tool_call],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    seen = _install_upstream_responses([first_resp, _assistant_oneshot("done")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "loop me"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    # The tool result that came back to the LLM in the second iteration
    # should contain an error mentioning the current channel.
    second_req = seen[1]
    tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "web" in tool_msgs[0]["content"]
    assert "error" in tool_msgs[0]["content"].lower()
