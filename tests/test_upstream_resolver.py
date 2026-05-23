"""Tests for the upstream-client resolver.

The resolver reads `llm_credentials` and rebuilds `app.state.upstream`
so the agent loop and the chat routes pick up the active credential
without restart. Lifespan does the first build at boot; the CRUD routes
trigger rebuilds on change.
"""
import secrets

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.crypto import EncryptedBlob, Encryptor
from hermes.main import app
from hermes.repository import llm_credentials as repo
from hermes.repository.models import LlmCredential
from hermes.upstream import (
    PROVIDER_DEFAULTS,
    UpstreamConfigError,
    build_client_for_credential,
)

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


# ─── pure resolver ──────────────────────────────────────────────────


def test_build_client_for_api_key_uses_provider_default() -> None:
    enc = Encryptor(secrets.token_bytes(32))
    ct = enc.encrypt("sk-test-123")
    cred = LlmCredential(
        id=1,
        provider="openai",
        mode="api_key",
        display_name="test",
        base_url=None,
        model=None,
        is_active=True,
        api_key_iv=ct.iv,
        api_key_tag=ct.tag,
        api_key_data=ct.data,
        oauth_status=None,
        oauth_authorized_at=None,
        oauth_iv=None,
        oauth_tag=None,
        oauth_data=None,
        created_at=0,
        updated_at=0,
    )
    client = build_client_for_credential(
        cred, encryptor=enc, fallback_proxy_url="http://proxy:8080"
    )
    assert client.headers["Authorization"] == "Bearer sk-test-123"
    assert str(client.base_url).rstrip("/") == PROVIDER_DEFAULTS["openai"].rstrip("/")


def test_build_client_for_api_key_respects_explicit_base_url() -> None:
    enc = Encryptor(secrets.token_bytes(32))
    ct = enc.encrypt("key-xyz")
    cred = LlmCredential(
        id=1, provider="custom", mode="api_key", display_name="t",
        base_url="https://my-mirror.example.com/v1", model=None, is_active=True,
        api_key_iv=ct.iv, api_key_tag=ct.tag, api_key_data=ct.data,
        oauth_status=None, oauth_authorized_at=None,
        oauth_iv=None, oauth_tag=None, oauth_data=None,
        created_at=0, updated_at=0,
    )
    client = build_client_for_credential(
        cred, encryptor=enc, fallback_proxy_url="http://proxy:8080"
    )
    assert str(client.base_url).rstrip("/") == "https://my-mirror.example.com/v1"
    assert client.headers["Authorization"] == "Bearer key-xyz"


def test_build_client_for_custom_provider_without_base_url_raises() -> None:
    enc = Encryptor(secrets.token_bytes(32))
    ct = enc.encrypt("k")
    cred = LlmCredential(
        id=1, provider="custom", mode="api_key", display_name="t",
        base_url=None, model=None, is_active=True,
        api_key_iv=ct.iv, api_key_tag=ct.tag, api_key_data=ct.data,
        oauth_status=None, oauth_authorized_at=None,
        oauth_iv=None, oauth_tag=None, oauth_data=None,
        created_at=0, updated_at=0,
    )
    with pytest.raises(UpstreamConfigError):
        build_client_for_credential(
            cred, encryptor=enc, fallback_proxy_url="http://proxy:8080"
        )


def test_build_client_for_oauth_claude_routes_to_proxy() -> None:
    enc = Encryptor(secrets.token_bytes(32))
    cred = LlmCredential(
        id=1, provider="anthropic", mode="oauth_claude", display_name="t",
        base_url=None, model=None, is_active=True,
        api_key_iv=None, api_key_tag=None, api_key_data=None,
        oauth_status="authorized", oauth_authorized_at=0,
        oauth_iv="aa", oauth_tag="bb", oauth_data="cc",
        created_at=0, updated_at=0,
    )
    client = build_client_for_credential(
        cred, encryptor=enc, fallback_proxy_url="http://haex-claude-proxy:8080"
    )
    assert str(client.base_url).rstrip("/") == "http://haex-claude-proxy:8080"
    # No Authorization header — proxy reads the OAuth creds itself via
    # its sqlite resolver plugin (Phase 5).
    assert "authorization" not in {k.lower() for k in client.headers}


def test_build_client_for_anthropic_api_key_routes_to_proxy() -> None:
    """Anthropic credentials always go through the haex-claude-proxy, even
    when stored as api_key — api.anthropic.com's native shape is
    /v1/messages (not OpenAI /v1/chat/completions) and `sk-ant-oat01-…`
    setup-token credentials are rate-limit-blocked when used as Bearer
    directly. The proxy reads the same DB row and handles both."""
    enc = Encryptor(secrets.token_bytes(32))
    ct = enc.encrypt("sk-ant-oat01-fake-test-token")
    cred = LlmCredential(
        id=1, provider="anthropic", mode="api_key", display_name="t",
        base_url=None, model=None, is_active=True,
        api_key_iv=ct.iv, api_key_tag=ct.tag, api_key_data=ct.data,
        oauth_status=None, oauth_authorized_at=0,
        oauth_iv=None, oauth_tag=None, oauth_data=None,
        created_at=0, updated_at=0,
    )
    client = build_client_for_credential(
        cred, encryptor=enc, fallback_proxy_url="http://haex-claude-proxy:8080"
    )
    assert str(client.base_url).rstrip("/") == "http://haex-claude-proxy:8080"
    # No Authorization header — the agent's runner_session token is what
    # the proxy reads, not the upstream credential.
    assert "authorization" not in {k.lower() for k in client.headers}


# ─── integration: CRUD endpoints rebuild app.state.upstream ──────────


async def test_lifespan_uses_env_fallback_when_no_active_credential(
    client: httpx.AsyncClient,
) -> None:
    # No active row → upstream points at the configured proxy URL.
    assert app.state.upstream is not None
    base = str(app.state.upstream.base_url).rstrip("/")
    assert base == "http://haex-claude-proxy:8080"


async def test_activate_credential_rebuilds_upstream(
    client: httpx.AsyncClient,
) -> None:
    create_resp = await client.post(
        "/api/llm/credentials",
        json={
            "provider": "openai",
            "display_name": "u-openai",
            "api_key": "sk-fresh-key",
        },
        headers=AUTH,
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    before = app.state.upstream
    activate_resp = await client.patch(
        f"/api/llm/credentials/{created['id']}/activate", headers=AUTH
    )
    assert activate_resp.status_code == 200
    after = app.state.upstream
    # New client instance.
    assert after is not before
    # New base_url is the OpenAI default.
    assert (
        str(after.base_url).rstrip("/") == PROVIDER_DEFAULTS["openai"].rstrip("/")
    )
    assert after.headers["Authorization"] == "Bearer sk-fresh-key"


async def test_delete_active_credential_falls_back_to_env(
    client: httpx.AsyncClient,
) -> None:
    create_resp = await client.post(
        "/api/llm/credentials",
        json={
            "provider": "openai",
            "display_name": "u",
            "api_key": "sk-tmp",
        },
        headers=AUTH,
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    activate_resp = await client.patch(
        f"/api/llm/credentials/{created['id']}/activate", headers=AUTH
    )
    assert activate_resp.status_code == 200
    delete_resp = await client.delete(
        f"/api/llm/credentials/{created['id']}", headers=AUTH
    )
    assert delete_resp.status_code == 204
    base = str(app.state.upstream.base_url).rstrip("/")
    # No active credential — back to the env-var proxy URL.
    assert base == "http://haex-claude-proxy:8080"
    assert "authorization" not in {k.lower() for k in app.state.upstream.headers}


async def test_creating_an_api_key_credential_does_not_activate_it(
    client: httpx.AsyncClient,
) -> None:
    # Creating a row leaves is_active=0 by default; the upstream should
    # NOT switch until the user explicitly activates.
    before = app.state.upstream
    create_resp = await client.post(
        "/api/llm/credentials",
        json={
            "provider": "openai",
            "display_name": "passive",
            "api_key": "sk-quiet",
        },
        headers=AUTH,
    )
    assert create_resp.status_code == 201
    assert app.state.upstream is before


async def test_oauth_authorize_rebuilds_when_active(
    client: httpx.AsyncClient,
) -> None:
    """OAuth credentials become active only when the user picks them
    via /activate. Until then the upstream stays on the previous
    fallback / api-key client."""
    # Insert an authorized oauth_claude row directly via the repo, then
    # activate it through the API. The agent should now route through
    # the proxy URL with no Authorization header.
    ct = app.state.encryptor.encrypt("dummy-creds-json")
    row = await repo.create_oauth_pending(
        app.state.db, display_name="claude-oauth-test"
    )
    await repo.update_oauth_authorized(
        app.state.db,
        cred_id=row.id,
        ciphertext=EncryptedBlob(iv=ct.iv, tag=ct.tag, data=ct.data),
        authorized_at=1,
    )
    activate_resp = await client.patch(
        f"/api/llm/credentials/{row.id}/activate", headers=AUTH
    )
    assert activate_resp.status_code == 200
    base = str(app.state.upstream.base_url).rstrip("/")
    assert base == "http://haex-claude-proxy:8080"
    assert "authorization" not in {k.lower() for k in app.state.upstream.headers}
