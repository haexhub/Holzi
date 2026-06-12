"""Tests for GET /api/chat/context."""
import httpx

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}




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
