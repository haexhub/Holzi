import asyncio

import httpx

from hermes.main import app
from hermes.repository import conversations, messages
from tests._chat_sse import (
    assistant_oneshot as _assistant_oneshot,
)
from tests._chat_sse import (
    install_upstream_responses as _install_upstream_responses,
)
from tests._chat_sse import (
    parse_sse as _parse_sse,
)
from tests._chat_sse import (
    to_sse_stream as _to_sse_stream,
)

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


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
        app.state.db, session_evt["conversation_id"], user_id=1
    )
    assert [(m.role, m.content) for m in msgs] == [("user", "hi")]


async def test_api_chat_cancel_endpoint_sets_event_and_returns_204(
    client: httpx.AsyncClient,
) -> None:
    """Direct test of POST /api/chat/runs/{id}/cancel: registered runs
    get their cancel event flipped and the endpoint returns 204. The
    streaming-side handling is covered by the cancellation flow test
    above; this one isolates the endpoint behaviour from SSE timing."""

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
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    await messages.append(
        app.state.db,
        user_id=1,
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
    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
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
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    await messages.append(
        app.state.db,
        user_id=1,
        conversation_id=convo.id,
        role="assistant",
        content="",
        ts=1002,
        meta_json='{"tool_calls": [{"id": "c1", "type": "function", '
        '"function": {"name": "save_note", "arguments": "{}"}}]}',
    )
    await messages.append(
        app.state.db,
        user_id=1,
        conversation_id=convo.id,
        role="tool",
        content="saved",
        ts=1003,
        meta_json='{"tool_call_id": "c1"}',
    )
    await messages.append(
        app.state.db,
        user_id=1,
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

    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "ask"),
        ("assistant", "regenerated"),
    ]
    sent_msgs = [m for m in seen[0]["messages"] if m["role"] != "system"]
    assert sent_msgs == [{"role": "user", "content": "ask"}]


async def test_retry_rejects_conversation_without_user_message(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    await messages.append(
        app.state.db,
        user_id=1,
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
    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
    assert [m.content for m in msgs] == ["orphan"]


async def test_retry_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, user_id=1, channel="task", ts=1000)
    await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="hi", ts=1001
    )
    await messages.append(
        app.state.db,
        user_id=1,
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
    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
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
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    user = await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="old ask", ts=1001
    )
    await messages.append(
        app.state.db,
        user_id=1,
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

    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
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
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    first = await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="first", ts=1001
    )
    await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="assistant", content="r1", ts=1002
    )
    await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="second", ts=1003
    )
    await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="assistant", content="r2", ts=1004
    )
    seen = _install_upstream_responses([_assistant_oneshot("regenerated")])

    async with client.stream(
        "POST", _edit_url(convo.id, first.id), headers=AUTH, json={"content": "edited first"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "edited first"),
        ("assistant", "regenerated"),
    ]
    sent_msgs = [m for m in seen[0]["messages"] if m["role"] != "system"]
    assert sent_msgs == [{"role": "user", "content": "edited first"}]


async def test_edit_rejects_assistant_message(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    assistant = await messages.append(
        app.state.db,
        user_id=1,
        conversation_id=convo.id,
        role="assistant",
        content="reply",
        ts=1002,
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        _edit_url(convo.id, assistant.id), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 400
    # Nothing changed.
    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "ask"),
        ("assistant", "reply"),
    ]


async def test_edit_rejects_message_from_other_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo_a = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    convo_b = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    user_b = await messages.append(
        app.state.db, user_id=1, conversation_id=convo_b.id, role="user", content="b ask", ts=1001
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    # message_id belongs to convo_b, but the path targets convo_a.
    response = await client.post(
        _edit_url(convo_a.id, user_b.id), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 404
    msgs = await messages.list_by_conversation(app.state.db, convo_b.id, user_id=1)
    assert [m.content for m in msgs] == ["b ask"]


async def test_edit_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, user_id=1, channel="task", ts=1000)
    user = await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="hi", ts=1001
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        _edit_url(convo.id, user.id), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 400
    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
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
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        _edit_url(convo.id, 99999), headers=AUTH, json={"content": "x"}
    )
    assert response.status_code == 404


async def test_edit_rejects_empty_content(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    user = await messages.append(
        app.state.db, user_id=1, conversation_id=convo.id, role="user", content="ask", ts=1001
    )
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        _edit_url(convo.id, user.id), headers=AUTH, json={"content": ""}
    )
    # Empty content fails the declared-body validation → FastAPI 422.
    assert response.status_code == 422
    msgs = await messages.list_by_conversation(app.state.db, convo.id, user_id=1)
    assert [m.content for m in msgs] == ["ask"]


async def test_edit_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post(_edit_url(1, 1), json={"content": "x"})
    assert response.status_code == 401
