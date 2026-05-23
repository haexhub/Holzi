"""Unit tests for the provider-side `/v1/models` listing helpers.

Every provider gets its own listing function in `hermes.provider_models`,
mirroring Specifyr's `server/shared/utils/provider-models.ts`. The
network call is faked via `httpx.MockTransport` so we don't touch real
provider APIs in CI.
"""
import secrets

import httpx
import pytest

from hermes.crypto import Encryptor
from hermes.provider_models import (
    ANTHROPIC_OAUTH_MODELS,
    ProviderModelsError,
    clear_cache,
    list_provider_models,
)
from hermes.repository.models import LlmCredential


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    clear_cache()


def _make_credential(
    *,
    provider: str,
    mode: str = "api_key",
    api_key: str | None = "sk-test",
    base_url: str | None = None,
    encryptor: Encryptor | None = None,
) -> tuple[LlmCredential, Encryptor]:
    enc = encryptor or Encryptor(secrets.token_bytes(32))
    iv = tag = data = None
    if api_key is not None and mode == "api_key":
        ct = enc.encrypt(api_key)
        iv, tag, data = ct.iv, ct.tag, ct.data
    return (
        LlmCredential(
            id=1,
            provider=provider,
            mode=mode,
            display_name="t",
            base_url=base_url,
            model=None,
            is_active=True,
            api_key_iv=iv,
            api_key_tag=tag,
            api_key_data=data,
            oauth_status="authorized" if mode == "oauth_claude" else None,
            oauth_authorized_at=None,
            oauth_iv=None,
            oauth_tag=None,
            oauth_data=None,
            created_at=0,
            updated_at=0,
        ),
        enc,
    )


def _stub_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


# ─── OpenAI ───────────────────────────────────────────────────────────


async def test_openai_returns_sorted_models() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5"}, {"id": "gpt-4o"}]},
        )

    cred, enc = _make_credential(provider="openai", api_key="sk-test-key")
    async with _stub_client(handler) as client:
        result = await list_provider_models(cred, http=client, encryptor=enc)
    assert seen["url"].endswith("/v1/models")
    assert seen["auth"] == "Bearer sk-test-key"
    assert [m.id for m in result] == ["gpt-4o", "gpt-5"]
    assert all(m.label == m.id for m in result)


async def test_openai_respects_explicit_base_url() -> None:
    seen_host: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_host["host"] = request.url.host
        seen_host["path"] = request.url.path
        return httpx.Response(200, json={"data": []})

    cred, enc = _make_credential(
        provider="openai", base_url="https://my-mirror.example.com/v1"
    )
    async with _stub_client(handler) as client:
        await list_provider_models(cred, http=client, encryptor=enc)
    assert seen_host["host"] == "my-mirror.example.com"
    # joinUrl-equivalent: trailing /v1 + /v1/models shouldn't double up.
    assert seen_host["path"] == "/v1/models"


async def test_openrouter_uses_bearer_with_correct_base() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={"data": [{"id": "anthropic/claude-sonnet-4-5", "name": "Claude Sonnet"}]},
        )

    cred, enc = _make_credential(provider="openrouter", api_key="sk-or-xyz")
    async with _stub_client(handler) as client:
        result = await list_provider_models(cred, http=client, encryptor=enc)
    assert seen["host"] == "openrouter.ai"
    assert seen["path"] == "/api/v1/models"
    assert seen["auth"] == "Bearer sk-or-xyz"
    assert result[0].label == "Claude Sonnet (anthropic/claude-sonnet-4-5)"


# ─── Anthropic ────────────────────────────────────────────────────────


async def test_anthropic_api_key_uses_x_api_key_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x-api-key"] = request.headers["x-api-key"]
        seen["version"] = request.headers["anthropic-version"]
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-opus-4-7", "display_name": "Claude Opus 4.7"}]},
        )

    cred, enc = _make_credential(provider="anthropic", api_key="sk-ant-x")
    async with _stub_client(handler) as client:
        result = await list_provider_models(cred, http=client, encryptor=enc)
    assert seen["x-api-key"] == "sk-ant-x"
    assert seen["version"]  # any non-empty value is fine
    assert result[0].id == "claude-opus-4-7"
    assert result[0].label == "Claude Opus 4.7 (claude-opus-4-7)"


async def test_anthropic_oauth_returns_curated_list_without_http_call() -> None:
    cred, enc = _make_credential(provider="anthropic", mode="oauth_claude", api_key=None)
    # Pass a client that would fail any call — proves we skip the network.
    def fail(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("OAuth path should not hit /v1/models")
    async with _stub_client(fail) as client:
        result = await list_provider_models(cred, http=client, encryptor=enc)
    assert result == ANTHROPIC_OAUTH_MODELS


# ─── Google ───────────────────────────────────────────────────────────


async def test_google_lists_only_generate_content_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Key is appended as ?key=…
        assert "key=" in str(request.url)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-embed-001",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )

    cred, enc = _make_credential(provider="google", api_key="AIzaSyTest")
    async with _stub_client(handler) as client:
        result = await list_provider_models(cred, http=client, encryptor=enc)
    assert [m.id for m in result] == ["gemini-2.5-pro"]
    assert result[0].label == "Gemini 2.5 Pro (gemini-2.5-pro)"


# ─── error paths ──────────────────────────────────────────────────────


async def test_api_key_credential_without_ciphertext_raises() -> None:
    cred, enc = _make_credential(provider="openai", api_key=None)
    async with _stub_client(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ProviderModelsError):
            await list_provider_models(cred, http=client, encryptor=enc)


async def test_oauth_provider_other_than_anthropic_raises() -> None:
    cred, enc = _make_credential(provider="openai", mode="oauth_claude", api_key=None)
    async with _stub_client(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ProviderModelsError):
            await list_provider_models(cred, http=client, encryptor=enc)


async def test_provider_non_200_raises_with_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    cred, enc = _make_credential(provider="openai", api_key="sk-x")
    async with _stub_client(handler) as client:
        with pytest.raises(ProviderModelsError, match="401"):
            await list_provider_models(cred, http=client, encryptor=enc)


async def test_provider_returns_non_json_body_raises() -> None:
    """200 with HTML/garbage body must surface as ProviderModelsError so the
    route maps it to 502, not 500."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>oops</html>",
            headers={"content-type": "text/html"},
        )

    cred, enc = _make_credential(provider="openai", api_key="sk-x")
    async with _stub_client(handler) as client:
        with pytest.raises(ProviderModelsError, match="non-JSON"):
            await list_provider_models(cred, http=client, encryptor=enc)


async def test_undecryptable_api_key_raises_provider_error() -> None:
    """If the stored ciphertext can't be decrypted (e.g. master key rotated),
    the error must be normalized to ProviderModelsError rather than bubbling
    up as a raw InvalidTag/ValueError that maps to 500."""
    enc1 = Encryptor(secrets.token_bytes(32))
    cred, _ = _make_credential(provider="openai", api_key="sk-x", encryptor=enc1)
    # Different encryptor → decrypt will fail.
    enc2 = Encryptor(secrets.token_bytes(32))
    async with _stub_client(lambda r: httpx.Response(200, json={"data": []})) as client:
        with pytest.raises(ProviderModelsError, match="decrypt"):
            await list_provider_models(cred, http=client, encryptor=enc2)


