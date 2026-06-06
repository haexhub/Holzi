"""Provider-side model listing.

Mirrors Specifyr's `server/shared/utils/provider-models.ts` — for each
supported provider, hit the provider's `/v1/models`-shaped endpoint with
the credential's decrypted API key and return `[{id, label}]`. The agent
loop doesn't care about the listing; this is purely for the settings UI
combobox.

Anthropic OAuth credentials don't carry an API key, so they fall back to
a curated list of Claude models. Bump the list when new families ship.
"""
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from hermes.crypto import EncryptedBlob, Encryptor
from hermes.repository.models import LlmCredential


class ProviderModelsError(RuntimeError):
    """Raised when the credential can't be turned into a model list —
    wrong mode, missing ciphertext, upstream non-200, network failure."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    id: str
    label: str
    # Provider-reported parameter list (OpenRouter only — surfaces
    # capabilities like "reasoning", "tools"). None when the provider
    # doesn't expose this metadata; downstream falls back to curated
    # rules in `hermes.thinking`.
    supported_parameters: tuple[str, ...] | None = None


# Curated fallback for `oauth_claude` credentials: Anthropic's /v1/models
# requires an API key, OAuth tokens are CLI-issued. Newest-first; tweak
# whenever a new model family ships.
ANTHROPIC_OAUTH_MODELS: tuple[ModelChoice, ...] = (
    ModelChoice(id="claude-opus-4-7", label="Claude Opus 4.7 (claude-opus-4-7)"),
    ModelChoice(id="claude-sonnet-4-6", label="Claude Sonnet 4.6 (claude-sonnet-4-6)"),
    ModelChoice(id="claude-haiku-4-5", label="Claude Haiku 4.5 (claude-haiku-4-5)"),
)


_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "openrouter": "https://openrouter.ai/api",
}


# ─── cache ────────────────────────────────────────────────────────────


_TTL_S = 10 * 60
_cache: dict[tuple[str, int], tuple[float, tuple[ModelChoice, ...]]] = {}


def clear_cache(cred_id: int | None = None) -> None:
    if cred_id is None:
        _cache.clear()
        return
    for key in list(_cache.keys()):
        if key[1] == cred_id:
            del _cache[key]


# ─── helpers ──────────────────────────────────────────────────────────


def _join_url(base: str, path: str) -> str:
    trimmed = base.rstrip("/")
    # Tolerate users pasting "https://openrouter.ai/api/v1" — without
    # this, base + "/v1/models" produces ".../v1/v1/models" and 404s.
    version_match = re.match(r"^/(v\d+[a-z]*)/", path)
    if version_match:
        version = version_match.group(1)
        if trimmed.endswith(f"/{version}"):
            trimmed = trimmed[: -(len(version) + 1)]
    return trimmed + (path if path.startswith("/") else "/" + path)


def _decrypt_api_key(cred: LlmCredential, encryptor: Encryptor) -> str:
    if cred.mode != "api_key":
        raise ProviderModelsError(
            "cannot list models for non api_key credentials; "
            "use the curated list for oauth_claude"
        )
    if not (cred.api_key_iv and cred.api_key_tag and cred.api_key_data):
        raise ProviderModelsError(
            f"credential {cred.id} is api_key but has no ciphertext"
        )
    try:
        return encryptor.decrypt(
            EncryptedBlob(
                iv=cred.api_key_iv, tag=cred.api_key_tag, data=cred.api_key_data
            )
        )
    except Exception as exc:
        raise ProviderModelsError(
            f"could not decrypt api key for credential {cred.id}"
        ) from exc


async def _fetch_json(
    http: httpx.AsyncClient, *, url: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    try:
        response = await http.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise ProviderModelsError(
            f"could not reach provider: {exc}"
        ) from exc
    if response.status_code >= 400:
        body = response.text[:300] if response.text else ""
        raise ProviderModelsError(
            f"provider returned {response.status_code}{f': {body}' if body else ''}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderModelsError(
            f"provider returned non-JSON body: {response.text[:300]}"
        ) from exc


# ─── per-provider listers ─────────────────────────────────────────────


async def _list_openai_like(
    cred: LlmCredential,
    *,
    http: httpx.AsyncClient,
    encryptor: Encryptor,
    default_base: str,
    capture_supported_parameters: bool = False,
) -> tuple[ModelChoice, ...]:
    """OpenAI + OpenRouter share the same wire format (`{data:[{id,name?}]}`)
    and the same `Authorization: Bearer …` header.

    OpenRouter additionally returns `supported_parameters: [...]` per
    model — when `capture_supported_parameters` is True it's preserved
    on `ModelChoice` so the capability layer can read it later."""
    key = _decrypt_api_key(cred, encryptor)
    base = cred.base_url or default_base
    data = await _fetch_json(
        http,
        url=_join_url(base, "/v1/models"),
        headers={"Authorization": f"Bearer {key}"},
    )
    raw = data.get("data") or []
    out: list[ModelChoice] = []
    for m in raw:
        mid = m.get("id")
        if not mid:
            continue
        name = m.get("name")
        sp: tuple[str, ...] | None = None
        if capture_supported_parameters:
            params = m.get("supported_parameters")
            if isinstance(params, list):
                sp = tuple(p for p in params if isinstance(p, str))
        out.append(
            ModelChoice(
                id=mid,
                label=f"{name} ({mid})" if name else mid,
                supported_parameters=sp,
            )
        )
    out.sort(key=lambda m: m.id)
    return tuple(out)


async def _list_anthropic(
    cred: LlmCredential,
    *,
    http: httpx.AsyncClient,
    encryptor: Encryptor,
) -> tuple[ModelChoice, ...]:
    if cred.mode == "oauth_claude":
        return ANTHROPIC_OAUTH_MODELS
    key = _decrypt_api_key(cred, encryptor)
    base = cred.base_url or _DEFAULT_BASE_URLS["anthropic"]
    data = await _fetch_json(
        http,
        url=_join_url(base, "/v1/models"),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    raw = data.get("data") or []
    out: list[ModelChoice] = []
    for m in raw:
        mid = m.get("id")
        if not mid:
            continue
        display = m.get("display_name")
        out.append(
            ModelChoice(id=mid, label=f"{display} ({mid})" if display else mid)
        )
    out.sort(key=lambda m: m.id)
    return tuple(out)


async def _list_google(
    cred: LlmCredential,
    *,
    http: httpx.AsyncClient,
    encryptor: Encryptor,
) -> tuple[ModelChoice, ...]:
    key = _decrypt_api_key(cred, encryptor)
    base = cred.base_url or _DEFAULT_BASE_URLS["google"]
    data = await _fetch_json(
        http, url=_join_url(base, f"/v1beta/models?key={key}")
    )
    raw = data.get("models") or []
    out: list[ModelChoice] = []
    for m in raw:
        methods = m.get("supportedGenerationMethods")
        if methods is not None and "generateContent" not in methods:
            continue
        name = m.get("name", "")
        mid = name[len("models/") :] if name.startswith("models/") else name
        if not mid:
            continue
        display = m.get("displayName")
        out.append(
            ModelChoice(id=mid, label=f"{display} ({mid})" if display else mid)
        )
    out.sort(key=lambda m: m.id)
    return tuple(out)


# ─── entry point ──────────────────────────────────────────────────────


_Provider = str
_Lister = Callable[
    [LlmCredential],
    Awaitable[tuple[ModelChoice, ...]],
]


async def list_provider_models(
    cred: LlmCredential,
    *,
    http: httpx.AsyncClient,
    encryptor: Encryptor,
    use_cache: bool = True,
) -> tuple[ModelChoice, ...]:
    """Resolve the model list for a credential. Caches per `(provider,
    cred_id)` for 10 min; pass `use_cache=False` to force a refresh."""
    if use_cache:
        hit = _cache.get((cred.provider, cred.id))
        if hit is not None and hit[0] > time.time():
            return hit[1]
    if cred.provider == "openai":
        models = await _list_openai_like(
            cred,
            http=http,
            encryptor=encryptor,
            default_base=_DEFAULT_BASE_URLS["openai"],
        )
    elif cred.provider == "openrouter":
        models = await _list_openai_like(
            cred,
            http=http,
            encryptor=encryptor,
            default_base=_DEFAULT_BASE_URLS["openrouter"],
            capture_supported_parameters=True,
        )
    elif cred.provider == "anthropic":
        models = await _list_anthropic(cred, http=http, encryptor=encryptor)
    elif cred.provider == "google":
        models = await _list_google(cred, http=http, encryptor=encryptor)
    else:
        raise ProviderModelsError(
            f"no model lister for provider '{cred.provider}'"
        )
    _cache[(cred.provider, cred.id)] = (time.time() + _TTL_S, models)
    return models
