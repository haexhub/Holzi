"""Tests for per-turn model + persona overrides on POST /api/chat."""
import json
import httpx
import pytest
from asgi_lifespan import LifespanManager
from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _sse_done_stream() -> bytes:
    """Minimal SSE stream: session + run + one text chunk + done."""
    chunks = [
        b'event: session\ndata: {"event":"session","version":1,"data":{"conversation_id":1}}\n\n',
        b'event: run\ndata: {"event":"run","version":1,"data":{"run_id":"r1"}}\n\n',
        b'event: text\ndata: {"event":"text","version":1,"data":{"content":"ok"}}\n\n',
        b'data: [DONE]\n\n',
        b'event: done\ndata: {"event":"done","version":1,"data":{}}\n\n',
    ]
    return b"".join(chunks)


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


@pytest.fixture(autouse=True)
def _mock_upstream(monkeypatch):
    """Intercept upstream calls and return a canned SSE stream."""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(_sse_done_stream()),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-upstream",
    )
    yield seen_bodies
    # credential fixture restores upstream in test teardown via LifespanManager


async def test_chat_request_accepts_model_override(client, _mock_upstream):
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "model_override": "claude-opus-4-7"},
    )
    # Stream consumes without error (2xx headers delivered)
    assert resp.status_code == 200


async def test_chat_request_accepts_persona_id_override(client, _mock_upstream):
    # persona_id=1 should exist (the default "Hermes" persona seeded at lifespan)
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "persona_id_override": 1},
    )
    assert resp.status_code == 200


async def test_chat_request_rejects_unknown_persona_id(client, _mock_upstream):
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "persona_id_override": 999999},
    )
    assert resp.status_code == 404
