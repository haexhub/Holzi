"""Tests for thinking_budget on POST /api/chat."""
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _sse_done_stream() -> bytes:
    return b"".join([
        b'event: session\ndata: {"event":"session","version":1,"data":{"conversation_id":1}}\n\n',
        b'event: run\ndata: {"event":"run","version":1,"data":{"run_id":"r1"}}\n\n',
        b'event: text\ndata: {"event":"text","version":1,"data":{"content":"ok"}}\n\n',
        b'data: [DONE]\n\n',
        b'event: done\ndata: {"event":"done","version":1,"data":{}}\n\n',
    ])


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
    """Install mock AFTER lifespan startup so we don't get clobbered."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
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
    if previous is not None:
        await previous.aclose()
    yield seen


async def test_thinking_budget_accepted(client, _mock_upstream):
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "thinking_budget": "medium"},
    )
    assert resp.status_code == 200


async def test_thinking_budget_invalid_value_rejected(client, _mock_upstream):
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hello", "thinking_budget": "extreme"},
    )
    # api_chat manually validates via ChatRequest.model_validate → ValueError → 400
    assert resp.status_code == 400


async def test_no_thinking_budget_omits_thinking_field(client, _mock_upstream):
    await client.post("/api/chat", headers=AUTH, json={"message": "hello"})
    assert len(_mock_upstream) > 0
    assert "thinking" not in _mock_upstream[-1]
