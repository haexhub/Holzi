import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.crypto import EncryptedBlob
from hermes.main import app
from hermes.repository import llm_credentials as repo

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


async def test_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/llm/credentials")
    assert response.status_code == 401


async def test_list_empty_when_no_rows(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/llm/credentials", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == []


async def test_create_api_key_returns_id_without_ciphertext(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/llm/credentials",
        json={
            "provider": "openai",
            "display_name": "Marko OpenAI",
            "api_key": "sk-test-1234",
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["provider"] == "openai"
    assert body["mode"] == "api_key"
    assert body["display_name"] == "Marko OpenAI"
    assert body["is_active"] is False
    # Ciphertext columns must not leak into the API.
    assert "api_key_data" not in body
    assert "api_key" not in body


async def test_create_persists_encrypted_blob(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/llm/credentials",
        json={"provider": "anthropic", "display_name": "x", "api_key": "secret-key-42"},
        headers=AUTH,
    )
    cred_id = response.json()["id"]
    row = await repo.get(app.state.db, cred_id)
    assert row is not None
    # Ciphertext stored, plaintext not — round-trip via the same encryptor
    # the lifespan booted up.
    blob = EncryptedBlob(iv=row.api_key_iv, tag=row.api_key_tag, data=row.api_key_data)
    assert app.state.encryptor.decrypt(blob) == "secret-key-42"


async def test_create_validates_provider(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/llm/credentials",
        json={"provider": "bogus", "display_name": "x", "api_key": "k"},
        headers=AUTH,
    )
    assert response.status_code == 422


async def test_create_requires_non_empty_api_key(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/llm/credentials",
        json={"provider": "openai", "display_name": "x", "api_key": ""},
        headers=AUTH,
    )
    assert response.status_code == 422


async def test_list_returns_newest_first(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/llm/credentials",
        json={"provider": "openai", "display_name": "A", "api_key": "k1"},
        headers=AUTH,
    )
    await client.post(
        "/api/llm/credentials",
        json={"provider": "anthropic", "display_name": "B", "api_key": "k2"},
        headers=AUTH,
    )
    response = await client.get("/api/llm/credentials", headers=AUTH)
    rows = response.json()
    assert [r["display_name"] for r in rows] == ["B", "A"]


async def test_delete_removes_row(client: httpx.AsyncClient) -> None:
    created = (
        await client.post(
            "/api/llm/credentials",
            json={"provider": "openai", "display_name": "tmp", "api_key": "k"},
            headers=AUTH,
        )
    ).json()
    response = await client.delete(f"/api/llm/credentials/{created['id']}", headers=AUTH)
    assert response.status_code == 204
    again = await client.delete(f"/api/llm/credentials/{created['id']}", headers=AUTH)
    assert again.status_code == 404


async def test_activate_flips_is_active_and_clears_others(client: httpx.AsyncClient) -> None:
    a = (
        await client.post(
            "/api/llm/credentials",
            json={"provider": "openai", "display_name": "A", "api_key": "k1"},
            headers=AUTH,
        )
    ).json()
    b = (
        await client.post(
            "/api/llm/credentials",
            json={"provider": "anthropic", "display_name": "B", "api_key": "k2"},
            headers=AUTH,
        )
    ).json()

    r1 = await client.patch(f"/api/llm/credentials/{a['id']}/activate", headers=AUTH)
    assert r1.status_code == 200
    assert r1.json()["is_active"] is True

    # Activating B must also deactivate A in the list output.
    r2 = await client.patch(f"/api/llm/credentials/{b['id']}/activate", headers=AUTH)
    assert r2.status_code == 200
    listed = (await client.get("/api/llm/credentials", headers=AUTH)).json()
    actives = {row["id"]: row["is_active"] for row in listed}
    assert actives == {a["id"]: False, b["id"]: True}


async def test_activate_missing_row_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.patch("/api/llm/credentials/9999/activate", headers=AUTH)
    assert response.status_code == 404
