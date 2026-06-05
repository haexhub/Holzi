import asyncio
import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.agent import ApprovalDecision
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
    """Parse a series of `event: name\\ndata: {...}\\n\\n` blocks.

    Every block carries the shared envelope `{event, version, data}`; this
    helper unwraps it and returns `(event_name, data_payload)` so tests assert
    against the inner payload. `_parse_sse_envelopes` exposes the raw envelope
    for tests that check the envelope contract itself."""
    return [(name, env.get("data", {})) for name, env in _parse_sse_envelopes(body)]


def _parse_sse_envelopes(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE blocks into `(sse_event_line, full_envelope)` pairs."""
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


async def test_api_chat_passes_composed_persona_channel_system_prompt(
    client: httpx.AsyncClient,
) -> None:
    """Plan 29-A end-to-end: /api/chat builds its system prompt via
    `get_effective_system_prompt("web", db)`. Two states verified:
    (a) fresh backfill → default Hermes + default web prompt,
    (b) customised via the public preferences endpoints → composition
    reflects the new persona + channel prompt."""
    from hermes.personas import (
        CHANNEL_REGISTRY,
        DEFAULT_PERSONA_AGENTS,
        DEFAULT_PERSONA_IDENTITY,
        DEFAULT_PERSONA_SOUL,
    )

    from hermes.personas import _BOOTSTRAP_HINT

    # (a) Default composition. Backfill seeds all three fragments
    # (Plan 36), so the resolver emits Soul → Identity → Agents
    # sections before the channel prompt. Plan 37: bootstrap hint is
    # appended because the fresh lifespan seeds users with
    # bootstrap_completed=0.
    seen = _install_upstream_responses([_assistant_oneshot("a")])
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "first"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
    sys_a = seen[0]["messages"][0]
    assert sys_a["role"] == "system"
    assert sys_a["content"] == (
        f"## Soul\n{DEFAULT_PERSONA_SOUL}\n\n"
        f"## Identity\n{DEFAULT_PERSONA_IDENTITY}\n\n"
        f"## Agents\n{DEFAULT_PERSONA_AGENTS}\n\n"
        f"{CHANNEL_REGISTRY['web']['default_prompt']}\n\n"
        f"{_BOOTSTRAP_HINT}"
    )

    # (b) Customised: new persona (identity-only) + custom channel prompt.
    new_persona = await client.post(
        "/api/personas",
        headers=AUTH,
        json={
            "name": "Reviewer",
            "identity": "Be merciless about types.",
        },
    )
    pid = new_persona.json()["id"]
    await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={
            "prompt": "Custom web prompt.",
            "default_persona_id": pid,
        },
    )

    seen = _install_upstream_responses([_assistant_oneshot("b")])
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "second"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass
    sys_b = seen[0]["messages"][0]
    assert sys_b["role"] == "system"
    assert sys_b["content"] == (
        "## Identity\nBe merciless about types.\n\n"
        f"Custom web prompt.\n\n"
        f"{_BOOTSTRAP_HINT}"
    )


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

    text_events = [d["content"] for name, d in _parse_sse(out) if name == "text"]

    assert text_events == ["Hello", " ", "world"]


async def test_api_chat_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    """Channel semantics: /api/chat is web-only and must not write into
    conversations belonging to other channels (e.g. scheduled-task runs)."""
    task_convo = await conversations.create(app.state.db, channel="task", ts=1000)
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hijack", "conversation_id": task_convo.id},
    )
    assert response.status_code == 400
    # Nothing should have been written into the task conversation.
    msgs = await messages.list_by_conversation(app.state.db, task_convo.id)
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
    assert "task_create" in names


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
    convo = await conversations.create(app.state.db, channel="task", ts=1000)
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
    # The task conversation is untouched.
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
    convo = await conversations.create(app.state.db, channel="task", ts=1000)
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
    # Empty content fails the declared-body validation → FastAPI 422.
    assert response.status_code == 422
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [m.content for m in msgs] == ["ask"]


async def test_edit_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post(_edit_url(1, 1), json={"content": "x"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Event envelope + tool call/result events (Plan 08)
# ---------------------------------------------------------------------------


def _tool_call_first_response(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "x",
        "model": "claude-opus-4-7",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_evt",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


async def test_api_chat_wraps_every_event_in_versioned_envelope(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("hi there")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    envelopes = _parse_sse_envelopes(body)
    assert [name for name, _ in envelopes] == ["session", "run", "text", "done"]
    for sse_event_line, env in envelopes:
        # SSE event line mirrors the envelope's `event` field.
        assert env["event"] == sse_event_line
        assert env["version"] == 1
        assert "data" in env


async def test_api_chat_emits_tool_call_and_result_events(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response("save_note", {"key": "k1", "content": "hello"}),
            _assistant_oneshot("saved it"),
        ]
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "save a note"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert names == ["session", "run", "tool_call", "tool_result", "text", "done"]

    tool_call = next(d for n, d in events if n == "tool_call")
    assert tool_call["call_id"] == "call_evt"
    assert tool_call["name"] == "save_note"
    assert tool_call["arguments"] == {"key": "k1", "content": "hello"}
    assert tool_call["status"] == "running"

    tool_result = next(d for n, d in events if n == "tool_result")
    assert tool_result["call_id"] == "call_evt"
    assert tool_result["status"] == "success"
    assert tool_result["result"]


async def test_conversation_detail_exposes_tool_call_metadata(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response("save_note", {"key": "k2", "content": "x"}),
            _assistant_oneshot("done"),
        ]
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "go"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    conv_id = _parse_sse(body)[0][1]["conversation_id"]

    detail = await client.get(f"/api/conversations/{conv_id}", headers=AUTH)
    assert detail.status_code == 200
    msgs = detail.json()["messages"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    tc = tool_msgs[0]["tool_call"]
    assert tc["call_id"] == "call_evt"
    assert tc["name"] == "save_note"
    assert tc["arguments"] == {"key": "k2", "content": "x"}
    assert tc["status"] == "success"
    assert tc["result"]
    assert tc["error"] is None
    # Non-tool messages carry a null tool_call.
    assert all(m.get("tool_call") is None for m in msgs if m["role"] != "tool")


# ---------------------------------------------------------------------------
# Reasoning (Plan 10)
# ---------------------------------------------------------------------------


def _install_reasoning_upstream() -> None:
    """Install an upstream that streams two reasoning deltas then the answer."""

    def _delta(delta: dict[str, Any]) -> bytes:
        chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        return f"data: {json.dumps(chunk)}\n\n".encode()

    body = (
        _delta({"reasoning_content": "Let me "})
        + _delta({"reasoning_content": "think."})
        + _delta({"content": "42"})
        + b"data: [DONE]\n\n"
    )

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


async def test_api_chat_emits_reasoning_events_and_persists(
    client: httpx.AsyncClient,
) -> None:
    _install_reasoning_upstream()

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "what is the answer"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    names = [name for name, _ in events]
    # Reasoning events stream before the text answer; chat still ends on done.
    assert names == ["session", "run", "reasoning", "reasoning", "text", "done"]
    reasoning = [d["content"] for n, d in events if n == "reasoning"]
    assert reasoning == ["Let me ", "think."]
    assert next(d for n, d in events if n == "text")["content"] == "42"

    # Reload reconstructs the reasoning from meta_json so the card re-renders.
    conv_id = events[0][1]["conversation_id"]
    detail = await client.get(f"/api/conversations/{conv_id}", headers=AUTH)
    assert detail.status_code == 200
    msgs = detail.json()["messages"]
    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert assistant["reasoning"] == "Let me think."
    # A plain user turn carries no reasoning.
    user = next(m for m in msgs if m["role"] == "user")
    assert user["reasoning"] is None


# ---------------------------------------------------------------------------
# Approvals (Plan 09)
# ---------------------------------------------------------------------------


async def _resolve_first_pending_approval(decision: str) -> str:
    """Wait for an approval future to appear on app.state.approvals and
    resolve it directly.

    httpx's ASGITransport buffers the whole SSE body before returning, so a
    real mid-stream POST /api/approvals isn't testable (same limitation the
    cancel-flow test documents). Resolving the future from a concurrent task
    exercises the emit→pause→resume path end-to-end; the endpoint itself is
    covered by the dedicated unit tests below.
    """
    for _ in range(500):
        for approval_id, future in list(app.state.approvals.items()):
            if not future.done():
                future.set_result(ApprovalDecision(decision=decision))  # type: ignore[arg-type]
                return approval_id
        await asyncio.sleep(0.01)
    raise AssertionError("no approval became pending")


async def test_api_chat_emits_approval_required_and_resumes_on_allow(
    client: httpx.AsyncClient,
) -> None:
    """A risky tool emits `approval_required`; on allow the agent runs it and
    the turn finishes with the normal tool_call/tool_result/text/done tail."""
    _install_upstream_responses(
        [
            _tool_call_first_response(
                "mcp_install",
                {
                    "name": "test-mcp",
                    "display_name": "Test",
                    "transport": "http",
                    "url": "http://127.0.0.1:1/mcp",
                },
            ),
            _assistant_oneshot("done"),
        ]
    )

    resolver = asyncio.create_task(_resolve_first_pending_approval("allow_once"))
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "ping me"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert "approval_required" in names
    # Approval precedes the tool call it gates.
    assert names.index("approval_required") < names.index("tool_call")
    assert names[-1] == "done"

    approval = next(d for n, d in events if n == "approval_required")
    assert approval["call_id"] == "call_evt"
    assert approval["name"] == "mcp_install"
    assert approval["arguments"] == {
        "name": "test-mcp",
        "display_name": "Test",
        "transport": "http",
        "url": "http://127.0.0.1:1/mcp",
    }
    assert approval["approval_id"]
    assert approval["reason"]

    # Registry drained once the decision landed.
    assert approval["approval_id"] not in app.state.approvals


async def test_api_chat_deny_skips_tool_and_feeds_denied_result(
    client: httpx.AsyncClient,
) -> None:
    """On deny the gated tool never runs; the LLM's next round sees a denied
    tool result and no tool_call event is emitted for it."""
    seen = _install_upstream_responses(
        [
            _tool_call_first_response(
                "mcp_install",
                {
                    "name": "test-mcp",
                    "display_name": "Test",
                    "transport": "http",
                    "url": "http://127.0.0.1:1/mcp",
                },
            ),
            _assistant_oneshot("ok, won't send"),
        ]
    )

    resolver = asyncio.create_task(_resolve_first_pending_approval("deny"))
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "ping me"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert "approval_required" in names
    # Denied: nothing executed, so no tool_call event was streamed.
    assert "tool_call" not in names

    # The second upstream round received a denied tool result.
    second_req = seen[1]
    tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "deni" in tool_msgs[0]["content"].lower()


async def test_approval_endpoint_resolves_future_and_returns_204(
    client: httpx.AsyncClient,
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    app.state.approvals["unit-approval"] = future
    try:
        resp = await client.post(
            "/api/approvals/unit-approval",
            headers=AUTH,
            json={"decision": "allow_once"},
        )
        assert resp.status_code == 204
        assert future.done()
        assert future.result().decision == "allow_once"
    finally:
        app.state.approvals.pop("unit-approval", None)


async def test_approval_endpoint_unknown_id_returns_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/approvals/nope", headers=AUTH, json={"decision": "allow_once"}
    )
    assert resp.status_code == 404


async def test_approval_endpoint_already_resolved_returns_409(
    client: httpx.AsyncClient,
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    future.set_result(ApprovalDecision(decision="allow_once"))
    app.state.approvals["done-approval"] = future
    try:
        resp = await client.post(
            "/api/approvals/done-approval",
            headers=AUTH,
            json={"decision": "deny"},
        )
        assert resp.status_code == 409
    finally:
        app.state.approvals.pop("done-approval", None)


async def test_approval_endpoint_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/approvals/anything", json={"decision": "allow_once"}
    )
    assert resp.status_code == 401


async def test_approval_endpoint_rejects_invalid_decision(
    client: httpx.AsyncClient,
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    app.state.approvals["bad-approval"] = future
    try:
        resp = await client.post(
            "/api/approvals/bad-approval",
            headers=AUTH,
            json={"decision": "maybe"},
        )
        assert resp.status_code == 422
        assert not future.done()
    finally:
        app.state.approvals.pop("bad-approval", None)
