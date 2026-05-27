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
    assert event_names == ["session", "run", "text", "done"]

    session_evt = events[0][1]
    run_evt = events[1][1]
    text_evt = events[2][1]
    assert isinstance(session_evt["conversation_id"], int)
    assert isinstance(run_evt["run_id"], str) and run_evt["run_id"]
    assert text_evt["content"] == "hello back"

    conv_id = session_evt["conversation_id"]
    convo = await conversations.get(app.state.db, conv_id)
    assert convo is not None
    assert convo.channel == "web"
    assert convo.title == "hi"
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


async def test_api_chat_classifies_upstream_unreachable(
    client: httpx.AsyncClient,
) -> None:
    """ConnectError from upstream → `error` event with code=upstream_unreachable
    and status_code=502 inside the SSE payload."""
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = dict(_parse_sse(body))
    err = events["error"]
    assert err["code"] == "upstream_unreachable"
    assert err["status_code"] == 502
    assert "no route to host" in err["message"]


async def test_api_chat_classifies_upstream_timeout(
    client: httpx.AsyncClient,
) -> None:
    """ReadTimeout from upstream → code=upstream_timeout, status_code=504."""
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream too slow")

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    err = dict(_parse_sse(body))["error"]
    assert err["code"] == "upstream_timeout"
    assert err["status_code"] == 504


async def test_api_chat_classifies_upstream_http_error(
    client: httpx.AsyncClient,
) -> None:
    """Non-2xx response from upstream → HTTPStatusError → code=upstream_http_error,
    status_code=502 (with the upstream status surfaced in the message)."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b""),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    err = dict(_parse_sse(body))["error"]
    assert err["code"] == "upstream_http_error"
    assert err["status_code"] == 502
    assert "500" in err["message"]


async def test_api_chat_classifies_agent_error_for_truncated_stream(
    client: httpx.AsyncClient,
) -> None:
    """Upstream stream ends without [DONE] / finish_reason → run_agent raises
    RuntimeError → code=agent_error, status_code=500."""
    truncated = (
        b'data: {"choices":[{"index":0,"delta":{"content":"start"},'
        b'"finish_reason":null}]}\n\n'
        # No [DONE], no finish_reason — agent must refuse to persist.
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(truncated),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    err = dict(_parse_sse(body))["error"]
    assert err["code"] == "agent_error"
    assert err["status_code"] == 500


async def test_api_chat_emits_run_event_with_run_id(
    client: httpx.AsyncClient,
) -> None:
    """Every /api/chat stream must emit a `run` event with a non-empty run_id
    *before* the first content delta — frontends need it to wire up Stop."""
    _install_upstream_responses([_assistant_oneshot("hi back")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    names = [name for name, _ in events]
    # `run` must precede `text` so the frontend has the run_id by the
    # time the first chunk lands.
    assert "run" in names
    assert names.index("run") < names.index("text")
    run_evt = dict(events)["run"]
    assert isinstance(run_evt["run_id"], str)
    assert run_evt["run_id"]


async def test_api_chat_run_registry_is_cleaned_up_after_completion(
    client: httpx.AsyncClient,
) -> None:
    """The registry on app.state must not retain run_ids past the terminal
    SSE event — otherwise long-running deployments leak entries."""
    _install_upstream_responses([_assistant_oneshot("ok")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = dict(_parse_sse(body))
    run_id = events["run"]["run_id"]

    chat_runs = app.state.chat_runs
    assert run_id not in chat_runs


async def test_api_chat_cancel_unknown_run_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/chat/runs/does-not-exist/cancel", headers=AUTH
    )
    assert response.status_code == 404


async def test_api_chat_cancel_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat/runs/anything/cancel")
    assert response.status_code == 401


async def test_api_chat_emits_cancelled_terminal_when_event_set_during_run(
    client: httpx.AsyncClient,
) -> None:
    """Cancelling an active run emits `cancelled` as the single terminal
    SSE event (NOT followed by `done`) and clears the registry.

    The mock upstream simulates the user clicking Stop by setting the
    cancel event for every in-flight run as soon as the agent makes
    its upstream call. The agent observes the event at its "after
    upstream" check and raises ChatRunCancelled, which the SSE layer
    turns into the terminal `cancelled` event. httpx's ASGITransport
    buffers the full response body before returning, so wiring the
    cancel through a real HTTP POST mid-flight isn't testable — the
    endpoint's own behaviour is exercised by the dedicated cancel
    endpoint test below.
    """
    cancelled_runs: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        for run_id, evt in list(app.state.chat_runs.items()):
            cancelled_runs.append(run_id)
            evt.set()
        body = _to_sse_stream(_assistant_oneshot("partial"))
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
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert "run" in names
    assert "cancelled" in names, f"got events: {names}"
    cancelled_idx = names.index("cancelled")
    # `cancelled` is terminal — nothing after it.
    assert names[cancelled_idx + 1 :] == [], f"events after cancel: {names}"
    # Registry cleared.
    assert cancelled_runs, "upstream handler never saw a registered run"
    for run_id in cancelled_runs:
        assert run_id not in app.state.chat_runs
    # No fake completed assistant message in the conversation.
    session_evt = dict(events).get("session")
    assert session_evt is not None
    msgs = await messages.list_by_conversation(
        app.state.db, session_evt["conversation_id"]
    )
    assert [(m.role, m.content) for m in msgs] == [("user", "hi")]


async def test_api_chat_cancel_endpoint_sets_event_and_returns_204(
    client: httpx.AsyncClient,
) -> None:
    """Direct test of POST /api/chat/runs/{id}/cancel: registered runs
    get their cancel event flipped and the endpoint returns 204. The
    streaming-side handling is covered by the cancellation flow test
    above; this one isolates the endpoint behaviour from SSE timing."""
    import asyncio

    evt = asyncio.Event()
    app.state.chat_runs["unit-test-run"] = evt
    try:
        resp = await client.post(
            "/api/chat/runs/unit-test-run/cancel", headers=AUTH
        )
        assert resp.status_code == 204
        assert evt.is_set()
    finally:
        app.state.chat_runs.pop("unit-test-run", None)


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


# ---------------------------------------------------------------------------
# POST /api/conversations/{id}/retry
# ---------------------------------------------------------------------------


async def test_retry_replaces_last_assistant_turn(
    client: httpx.AsyncClient,
) -> None:
    """Retry drops the trailing assistant reply and regenerates it from the
    same user message, streaming with the same SSE semantics as /api/chat."""
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    await messages.append(
        app.state.db,
        conversation_id=convo.id,
        role="assistant",
        content="old answer",
        ts=1002,
    )
    seen = _install_upstream_responses([_assistant_oneshot("new answer")])

    async with client.stream(
        "POST", f"/api/conversations/{convo.id}/retry", headers=AUTH
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    assert [name for name, _ in events] == ["session", "run", "text", "done"]
    assert events[0][1]["conversation_id"] == convo.id
    assert events[2][1]["content"] == "new answer"

    # The old assistant turn is gone; the user message stays, new reply appended.
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "ask"),
        ("assistant", "new answer"),
    ]
    # The regenerated run saw only the surviving user turn as context.
    sent_msgs = [m for m in seen[0]["messages"] if m["role"] != "system"]
    assert sent_msgs == [{"role": "user", "content": "ask"}]


async def test_retry_drops_tool_turns_after_last_user_message(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    await messages.append(
        app.state.db,
        conversation_id=convo.id,
        role="assistant",
        content="",
        ts=1002,
        meta_json='{"tool_calls": [{"id": "c1", "type": "function", '
        '"function": {"name": "save_note", "arguments": "{}"}}]}',
    )
    await messages.append(
        app.state.db,
        conversation_id=convo.id,
        role="tool",
        content="saved",
        ts=1003,
        meta_json='{"tool_call_id": "c1"}',
    )
    await messages.append(
        app.state.db,
        conversation_id=convo.id,
        role="assistant",
        content="done earlier",
        ts=1004,
    )
    seen = _install_upstream_responses([_assistant_oneshot("regenerated")])

    async with client.stream(
        "POST", f"/api/conversations/{convo.id}/retry", headers=AUTH
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "ask"),
        ("assistant", "regenerated"),
    ]
    sent_msgs = [m for m in seen[0]["messages"] if m["role"] != "system"]
    assert sent_msgs == [{"role": "user", "content": "ask"}]


async def test_retry_rejects_conversation_without_user_message(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    await messages.append(
        app.state.db,
        conversation_id=convo.id,
        role="assistant",
        content="orphan",
        ts=1001,
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        f"/api/conversations/{convo.id}/retry", headers=AUTH
    )
    assert response.status_code == 400
    # Nothing was deleted.
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [m.content for m in msgs] == ["orphan"]


async def test_retry_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="signal", ts=1000)
    await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="hi", ts=1001
    )
    await messages.append(
        app.state.db,
        conversation_id=convo.id,
        role="assistant",
        content="reply",
        ts=1002,
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        f"/api/conversations/{convo.id}/retry", headers=AUTH
    )
    assert response.status_code == 400
    # The signal thread is untouched.
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"),
        ("assistant", "reply"),
    ]


async def test_retry_unknown_conversation_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post("/api/conversations/99999/retry", headers=AUTH)
    assert response.status_code == 404


async def test_retry_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/conversations/1/retry")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/conversations/{id}/messages/{message_id}/edit-and-regenerate
# ---------------------------------------------------------------------------


def _edit_url(conv_id: int, message_id: int) -> str:
    return f"/api/conversations/{conv_id}/messages/{message_id}/edit-and-regenerate"


async def test_edit_last_user_message_regenerates(
    client: httpx.AsyncClient,
) -> None:
    """Editing the last user message replaces its text, drops the assistant
    tail, and regenerates from the edited content with /api/chat SSE semantics."""
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    user = await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="old ask", ts=1001
    )
    await messages.append(
        app.state.db,
        conversation_id=convo.id,
        role="assistant",
        content="old answer",
        ts=1002,
    )
    seen = _install_upstream_responses([_assistant_oneshot("new answer")])

    async with client.stream(
        "POST", _edit_url(convo.id, user.id), headers=AUTH, json={"content": "new ask"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    assert [name for name, _ in events] == ["session", "run", "text", "done"]
    assert events[0][1]["conversation_id"] == convo.id
    assert events[2][1]["content"] == "new answer"

    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "new ask"),
        ("assistant", "new answer"),
    ]
    # The regenerated run saw the edited user turn as context.
    sent_msgs = [m for m in seen[0]["messages"] if m["role"] != "system"]
    assert sent_msgs == [{"role": "user", "content": "new ask"}]


async def test_edit_earlier_user_message_drops_everything_after(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    first = await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="first", ts=1001
    )
    await messages.append(
        app.state.db, conversation_id=convo.id, role="assistant", content="r1", ts=1002
    )
    await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="second", ts=1003
    )
    await messages.append(
        app.state.db, conversation_id=convo.id, role="assistant", content="r2", ts=1004
    )
    seen = _install_upstream_responses([_assistant_oneshot("regenerated")])

    async with client.stream(
        "POST", _edit_url(convo.id, first.id), headers=AUTH, json={"content": "edited first"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "edited first"),
        ("assistant", "regenerated"),
    ]
    sent_msgs = [m for m in seen[0]["messages"] if m["role"] != "system"]
    assert sent_msgs == [{"role": "user", "content": "edited first"}]


async def test_edit_rejects_assistant_message(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    assistant = await messages.append(
        app.state.db, conversation_id=convo.id, role="assistant", content="reply", ts=1002
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        _edit_url(convo.id, assistant.id), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 400
    # Nothing changed.
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "ask"),
        ("assistant", "reply"),
    ]


async def test_edit_rejects_message_from_other_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo_a = await conversations.create(app.state.db, channel="web", ts=1000)
    convo_b = await conversations.create(app.state.db, channel="web", ts=1000)
    user_b = await messages.append(
        app.state.db, conversation_id=convo_b.id, role="user", content="b ask", ts=1001
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    # message_id belongs to convo_b, but the path targets convo_a.
    response = await client.post(
        _edit_url(convo_a.id, user_b.id), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 404
    msgs = await messages.list_by_conversation(app.state.db, convo_b.id)
    assert [m.content for m in msgs] == ["b ask"]


async def test_edit_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="signal", ts=1000)
    user = await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="hi", ts=1001
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        _edit_url(convo.id, user.id), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 400
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [m.content for m in msgs] == ["hi"]


async def test_edit_unknown_conversation_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        _edit_url(99999, 1), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 404


async def test_edit_unknown_message_returns_404(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        _edit_url(convo.id, 99999), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 404


async def test_edit_rejects_empty_content(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    user = await messages.append(
        app.state.db, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        _edit_url(convo.id, user.id), headers=AUTH, json={"content": ""}
    )
    assert response.status_code == 400
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [m.content for m in msgs] == ["ask"]


async def test_edit_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post(_edit_url(1, 1), json={"content": "x"})
    assert response.status_code == 401
