import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import conversations, messages

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _non_stream_handler(content: str = "canned reply"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "claude-opus-4-7",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
        )

    return handler


def _stream_handler(deltas: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        events = []
        for d in deltas:
            chunk = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}],
            }
            events.append(f"data: {json.dumps(chunk)}\n\n".encode())
        events.append(b"data: [DONE]\n\n")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(events)),
        )

    return handler


def _install_upstream(handler):
    transport = httpx.MockTransport(handler)
    app.state.upstream = httpx.AsyncClient(transport=transport, base_url="http://fake-proxy")


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


async def test_chat_completions_requires_auth(client: httpx.AsyncClient) -> None:
    _install_upstream(_non_stream_handler())
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "claude-opus-4-7", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 401


async def test_chat_completions_non_streaming_creates_conversation_and_persists(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler(content="hello back"))
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello back"
    session_id = int(response.headers["x-hermes-session"])

    msgs = await messages.list_by_conversation(app.state.db, session_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["hi", "hello back"]


async def test_chat_completions_uses_existing_session_header(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler())
    convo = await conversations.create(app.state.db, channel="vscode", ts=1000)

    response = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Hermes-Session": str(convo.id)},
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "round 2"}],
        },
    )
    assert response.status_code == 200
    assert int(response.headers["x-hermes-session"]) == convo.id

    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [m.content for m in msgs] == ["round 2", "canned reply"]


async def test_chat_completions_unknown_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler())
    response = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Hermes-Session": "99999"},
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "CHAT_SESSION_NOT_FOUND"


async def test_chat_completions_invalid_session_header_returns_400(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler())
    response = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Hermes-Session": "not-a-number"},
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "CHAT_INVALID_SESSION"


async def test_chat_completions_streaming_passes_sse_and_persists(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_stream_handler(deltas=["Hello", " ", "world"]))

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        session_id = int(response.headers["x-hermes-session"])
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    # SSE pass-through
    assert b"data: [DONE]" in body
    assert b"Hello" in body and b"world" in body

    msgs = await messages.list_by_conversation(app.state.db, session_id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].content == "Hello world"


async def test_chat_completions_malformed_json_returns_400(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler())
    response = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "Content-Type": "application/json"},
        content=b"not-json-at-all",
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "REQUEST_INVALID_JSON"


async def test_chat_completions_upstream_connect_error_returns_502(
    client: httpx.AsyncClient,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection refused")

    _install_upstream(handler)
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "CHAT_UPSTREAM_UNREACHABLE"


async def test_chat_completions_upstream_timeout_returns_504(
    client: httpx.AsyncClient,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    _install_upstream(handler)
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 504
    assert response.json()["detail"] == "CHAT_UPSTREAM_TIMEOUT"


async def test_chat_completions_upstream_returns_non_json_2xx_returns_502(
    client: httpx.AsyncClient,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>cloudflare maintenance</html>", headers={"content-type": "text/html"}
        )

    _install_upstream(handler)
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "CHAT_UPSTREAM_NON_JSON"


async def test_chat_completions_streaming_with_upstream_5xx_returns_error_not_stream(
    client: httpx.AsyncClient,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream down"})

    _install_upstream(handler)
    response = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    # Caller should NOT see a 200 + SSE-wrapped error; they see the actual status.
    assert response.status_code == 503
    assert not response.headers.get("content-type", "").startswith("text/event-stream")


async def test_chat_completions_sticky_session_resumes_same_workspace(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler(content="first"))
    r1 = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Holzi-Workspace": "my-project"},
        json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r1.status_code == 200
    session_id = int(r1.headers["x-hermes-session"])

    _install_upstream(_non_stream_handler(content="second"))
    r2 = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Holzi-Workspace": "my-project"},
        json={"model": "m", "messages": [{"role": "user", "content": "follow-up"}]},
    )
    assert r2.status_code == 200
    assert int(r2.headers["x-hermes-session"]) == session_id

    msgs = await messages.list_by_conversation(app.state.db, session_id)
    assert [m.content for m in msgs] == ["hello", "first", "follow-up", "second"]


async def test_chat_completions_different_workspaces_get_different_sessions(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler())
    r1 = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Holzi-Workspace": "project-a"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi a"}]},
    )
    _install_upstream(_non_stream_handler())
    r2 = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Holzi-Workspace": "project-b"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi b"}]},
    )
    assert int(r1.headers["x-hermes-session"]) != int(r2.headers["x-hermes-session"])


async def test_chat_completions_no_workspace_header_is_sticky(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler())
    r1 = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    _install_upstream(_non_stream_handler())
    r2 = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "m", "messages": [{"role": "user", "content": "hi again"}]},
    )
    assert int(r1.headers["x-hermes-session"]) == int(r2.headers["x-hermes-session"])


async def test_chat_completions_explicit_session_overrides_sticky(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream(_non_stream_handler())
    r1 = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Holzi-Workspace": "ws"},
        json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
    )
    sticky_id = int(r1.headers["x-hermes-session"])

    other = await conversations.create(app.state.db, channel="cline", ts=1000)

    _install_upstream(_non_stream_handler())
    r2 = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Hermes-Session": str(other.id)},
        json={"model": "m", "messages": [{"role": "user", "content": "direct"}]},
    )
    assert r2.status_code == 200
    assert int(r2.headers["x-hermes-session"]) == other.id
    assert other.id != sticky_id
