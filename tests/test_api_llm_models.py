"""Integration tests for `GET /api/llm/credentials/{id}/models`.

We swap `app.state.external_http` for an `httpx.AsyncClient` backed by a
`MockTransport` so the route's path through `provider_models.list_provider_models`
exercises the real serialization without touching the network.
"""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.provider_models import clear_cache

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
        clear_cache()
        yield c


def _install_mock_external_http(handler) -> None:
    """Replace `app.state.external_http` so the GET /models route hits
    our fake transport instead of the public internet."""
    app.state.external_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=5.0
    )


async def _make_credential(client: httpx.AsyncClient, provider: str) -> dict:
    return (
        await client.post(
            "/api/llm/credentials",
            json={"provider": provider, "display_name": provider, "api_key": "k"},
            headers=AUTH,
        )
    ).json()


async def test_models_openai_returns_sorted_list(client: httpx.AsyncClient) -> None:
    _install_mock_external_http(
        lambda r: httpx.Response(
            200, json={"data": [{"id": "gpt-5"}, {"id": "gpt-4o"}]}
        )
    )
    cred = await _make_credential(client, "openai")
    r = await client.get(
        f"/api/llm/credentials/{cred['id']}/models", headers=AUTH
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["id"] for m in body["models"]] == ["gpt-4o", "gpt-5"]


async def test_models_anthropic_oauth_uses_curated_list(
    client: httpx.AsyncClient,
) -> None:
    # Insert an authorized oauth_claude row directly via the repo so we
    # can hit the curated-list code path without driving the OAuth flow.
    from hermes.crypto import EncryptedBlob
    from hermes.repository import llm_credentials as repo

    ct = app.state.encryptor.encrypt("fake-creds")
    row = await repo.create_oauth_pending(
        app.state.db, display_name="claude-oauth"
    )
    await repo.update_oauth_authorized(
        app.state.db,
        cred_id=row.id,
        ciphertext=EncryptedBlob(iv=ct.iv, tag=ct.tag, data=ct.data),
        authorized_at=1,
    )
    # Any HTTP attempt would fail this test — proves we skip the network.
    def fail(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("oauth_claude should not hit the network")

    _install_mock_external_http(fail)
    r = await client.get(
        f"/api/llm/credentials/{row.id}/models", headers=AUTH
    )
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["models"]]
    assert "claude-opus-4-7" in ids


async def test_models_unknown_credential_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_mock_external_http(lambda r: httpx.Response(200, json={"data": []}))
    r = await client.get(
        "/api/llm/credentials/9999/models", headers=AUTH
    )
    assert r.status_code == 404


async def test_models_provider_error_becomes_502(
    client: httpx.AsyncClient,
) -> None:
    _install_mock_external_http(
        lambda r: httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    cred = await _make_credential(client, "openai")
    r = await client.get(
        f"/api/llm/credentials/{cred['id']}/models", headers=AUTH
    )
    assert r.status_code == 502
    assert "401" in r.json()["detail"]


async def test_models_requires_auth(client: httpx.AsyncClient) -> None:
    cred = await _make_credential(client, "openai")
    r = await client.get(f"/api/llm/credentials/{cred['id']}/models")
    assert r.status_code == 401
