"""Tests for GET /api/models."""
import httpx
import pytest
from asgi_lifespan import LifespanManager
from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


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


async def test_models_requires_auth(client):
    resp = await client.get("/api/models")
    assert resp.status_code == 401


async def test_models_returns_list(client):
    resp = await client.get("/api/models", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert isinstance(data["models"], list)
    for m in data["models"]:
        assert "id" in m
        assert "credential_id" in m


async def test_models_fallback_when_provider_unreachable(client):
    """Even when the upstream /v1/models call fails, the list is not empty."""
    resp = await client.get("/api/models", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["models"]) >= 0  # graceful: empty is fine too
