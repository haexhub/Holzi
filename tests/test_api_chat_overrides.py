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
async def _mock_upstream(client):
    """Intercept upstream calls and return a canned SSE stream.

    Depends on `client` so it installs the mock AFTER the lifespan startup
    that sets `app.state.upstream` to a real client — otherwise startup
    would clobber the mock and requests would hit the real network.
    """
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(_sse_done_stream()),
        )

    previous = app.state.upstream
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-upstream",
    )
    # Close the real client the lifespan built at startup; we replaced it.
    if previous is not None:
        await previous.aclose()
    yield seen_bodies


async def test_chat_request_accepts_model_override(client, _mock_upstream):
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "model_override": "claude-opus-4-7"},
    )
    # Stream consumes without error (2xx headers delivered)
    assert resp.status_code == 200
    # The override must actually reach the upstream request — a bare 200
    # would also pass if the upstream call had silently failed.
    assert _mock_upstream, "upstream was never called"
    assert _mock_upstream[0]["model"] == "claude-opus-4-7"


async def test_chat_request_accepts_persona_id_override(client, _mock_upstream):
    # persona_id=1 should exist (the default "Hermes" persona seeded at lifespan)
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "persona_id_override": 1},
    )
    assert resp.status_code == 200
    # A valid persona override resolves and the turn reaches the upstream.
    assert _mock_upstream, "upstream was never called"


async def test_chat_request_rejects_unknown_persona_id(client, _mock_upstream):
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "persona_id_override": 999999},
    )
    assert resp.status_code == 404
