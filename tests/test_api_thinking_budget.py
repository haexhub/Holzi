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


def _patch_persona_to(monkeypatch, *, provider: str):
    """Swap the autouse persona-context fixture for one that reports the
    requested provider. Routes `run_agent` via that credential so
    `build_thinking_payload` sees the right wire format."""
    from hermes.personas import PersonaContext
    from hermes.repository.models import LlmCredential
    from hermes.routes import api as api_mod

    cred = LlmCredential(
        id=0,
        provider=provider,
        mode="api_key",
        display_name=f"test-{provider}",
        base_url=None,
        model=None,
        is_active=True,
        api_key_iv=b"",
        api_key_tag=b"",
        api_key_data=b"",
        oauth_status=None,
        oauth_authorized_at=None,
        oauth_iv=None,
        oauth_tag=None,
        oauth_data=None,
        created_at=0,
        updated_at=0,
    )

    async def _resolve(
        channel, engine, *, user_id, model_override=None, persona_id_override=None
    ):
        return PersonaContext(
            system_prompt="sys",
            credential=cred,
            model=model_override or "o1-mini",
        )

    monkeypatch.setattr(api_mod, "resolve_persona_context", _resolve)


async def test_thinking_budget_openai_uses_reasoning_effort(
    client, _mock_upstream, monkeypatch
):
    """OpenAI credentials must send reasoning_effort, never the Anthropic
    `thinking` block."""
    _patch_persona_to(monkeypatch, provider="openai")
    await client.post(
        "/api/chat",
        headers=AUTH,
        json={
            "message": "hello",
            "thinking_budget": "medium",
            "model_override": "o1-mini",
        },
    )
    assert len(_mock_upstream) > 0
    sent = _mock_upstream[-1]
    assert sent.get("reasoning_effort") == "medium"
    assert "thinking" not in sent


async def test_thinking_budget_openai_unsupported_model_silently_dropped(
    client, _mock_upstream, monkeypatch
):
    """A budget aimed at gpt-4o (no reasoning support) must NOT inject
    reasoning_effort — the provider would 400 on it."""
    _patch_persona_to(monkeypatch, provider="openai")
    await client.post(
        "/api/chat",
        headers=AUTH,
        json={
            "message": "hello",
            "thinking_budget": "high",
            "model_override": "gpt-4o",
        },
    )
    assert len(_mock_upstream) > 0
    sent = _mock_upstream[-1]
    assert "reasoning_effort" not in sent
    assert "thinking" not in sent


async def test_thinking_budget_anthropic_includes_max_tokens(
    client, _mock_upstream
):
    """The Anthropic path must add max_tokens > budget_tokens; otherwise
    the upstream rejects the request."""
    resp = await client.post(
        "/api/chat",
        headers=AUTH,
        json={
            "message": "hello",
            "thinking_budget": "high",
            "model_override": "claude-opus-4-7",
        },
    )
    assert resp.status_code == 200
    sent = _mock_upstream[-1]
    assert sent["thinking"]["budget_tokens"] == 16000
    assert sent["max_tokens"] > 16000
