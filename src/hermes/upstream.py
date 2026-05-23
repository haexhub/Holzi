"""Resolve the outgoing HTTP client from the active `llm_credentials` row.

The chat routes and the agent loop both go through `app.state.upstream`,
a long-lived `httpx.AsyncClient`. Lifespan builds the first one from env
vars (the legacy `HERMES_LLM_URL` / `HERMES_LLM_API_KEY` path) and then
asks `rebuild_upstream_from_db` to upgrade to whatever DB credential is
active. The credential-CRUD routes call the same function on every
mutating change so the active credential propagates without restart.

Routing rules:
- `api_key` mode → direct to `cred.base_url` if set, otherwise the
  hard-coded `PROVIDER_DEFAULTS[cred.provider]`. Bearer header carries
  the decrypted key.
- `oauth_claude` mode → proxy URL (the OAuth creds live in the DB and
  the proxy's sqlite resolver reads them; Hermes never sees plaintext).
- No active credential → fall back to env vars.
"""
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import EncryptedBlob, Encryptor
from hermes.repository import llm_credentials as repo
from hermes.repository.models import LlmCredential

# OpenAI-compatible base URLs per provider. Origin-only — `httpx`'s
# base_url join APPENDS paths rather than replacing them, and the agent
# already prepends `/v1/chat/completions` to every request. A trailing
# `/v1` here would have produced `…/v1/v1/chat/completions` and 404'd.
# A self-hosted mirror (LiteLLM, custom proxy) always overrides via
# `base_url` on the credential row.
PROVIDER_DEFAULTS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "openrouter": "https://openrouter.ai/api",
    # Google's OpenAI-compatible surface lives off /v1beta/openai, so the
    # full path the agent hits ends up `/v1beta/openai/v1/chat/completions`
    # — that's how Google ships it.
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
}


class UpstreamConfigError(RuntimeError):
    """The active credential row can't be turned into a usable client
    (missing ciphertext, custom provider without `base_url`, etc.)."""


def build_client_for_credential(
    cred: LlmCredential,
    *,
    encryptor: Encryptor,
    fallback_proxy_url: str,
) -> httpx.AsyncClient:
    # Anthropic ALWAYS goes through the haex-claude-proxy — both for legacy
    # oauth_claude credentials (credentials.json + claude-CLI spawn) and for
    # the new setup-token api_keys (sk-ant-oat01-…). api.anthropic.com's
    # native shape is /v1/messages, the agent speaks OpenAI-compatible
    # /v1/chat/completions, and oat01 tokens are rate-limit-blocked when
    # used as Bearer directly against api.anthropic.com — the proxy handles
    # all three translations and reads its own credential out of the same
    # DB row.
    if cred.provider == "anthropic":
        return httpx.AsyncClient(base_url=fallback_proxy_url, timeout=60.0)

    if cred.mode == "api_key":
        if not (cred.api_key_iv and cred.api_key_tag and cred.api_key_data):
            raise UpstreamConfigError(
                f"credential {cred.id} is api_key but has no ciphertext"
            )
        plaintext = encryptor.decrypt(
            EncryptedBlob(
                iv=cred.api_key_iv,
                tag=cred.api_key_tag,
                data=cred.api_key_data,
            )
        )
        base_url = cred.base_url or PROVIDER_DEFAULTS.get(cred.provider)
        if base_url is None:
            raise UpstreamConfigError(
                f"credential {cred.id} provider '{cred.provider}' "
                "has no default base_url; set it on the credential row"
            )
        return httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {plaintext}"},
            timeout=60.0,
        )
    if cred.mode == "oauth_claude":
        # Legacy path (pre-setup-token migration). OAuth credentials are
        # never decrypted here — they're handed to the proxy's sqlite
        # resolver.
        return httpx.AsyncClient(base_url=fallback_proxy_url, timeout=60.0)
    raise UpstreamConfigError(f"unknown credential mode: {cred.mode}")


def build_fallback_client(
    *, llm_url: str, llm_api_key: str
) -> httpx.AsyncClient:
    """Legacy env-var path. Lives here (not in main.py) so callers don't
    have to know about `main`'s import graph just to build a client."""
    headers = {"Authorization": f"Bearer {llm_api_key}"} if llm_api_key else None
    return httpx.AsyncClient(base_url=llm_url, headers=headers, timeout=60.0)


async def rebuild_upstream_from_db(
    app: FastAPI,
    *,
    db: AsyncEngine,
    encryptor: Encryptor,
    fallback_llm_url: str,
    fallback_llm_api_key: str,
) -> None:
    """Swap `app.state.upstream` for the client matching the currently
    active credential (or the env-var fallback when there is none).

    Closes the previously installed client so connection pools don't
    pile up across rebuilds. Safe to call concurrently with in-flight
    requests on the old client — httpx keeps the underlying TCP sockets
    open until the requests finish."""
    active = await repo.get_active(db)
    if active is None:
        new_client = build_fallback_client(
            llm_url=fallback_llm_url, llm_api_key=fallback_llm_api_key
        )
    else:
        new_client = build_client_for_credential(
            active, encryptor=encryptor, fallback_proxy_url=fallback_llm_url
        )

    old_client = getattr(app.state, "upstream", None)
    app.state.upstream = new_client
    if old_client is not None:
        await old_client.aclose()
