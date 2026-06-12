"""Tests for GET /api/chat/context."""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
async def client(pg_db):
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


async def test_chat_context_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/chat/context")
    assert resp.status_code == 401


async def test_chat_context_returns_persona_and_model(client: httpx.AsyncClient) -> None:
    """After lifespan boot the default 'Hermes' persona + configured model are visible."""
    resp = await client.get("/api/chat/context", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "persona_name" in data
    assert "model" in data
    assert isinstance(data["model"], str)
    assert data["model"]  # non-empty
    assert data["persona_name"] == "Hermes"  # default seed persona
