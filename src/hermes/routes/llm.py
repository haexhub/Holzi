"""HTTP API for `llm_credentials`.

CRUD over AES-GCM-encrypted credentials the agent loop and the haex-
claude-proxy sqlite-resolver pick up. Ciphertext columns never leak into
the API surface — `LlmCredentialResponse` is the only shape clients see.

Endpoints (all bearer-gated):
    GET    /api/llm/credentials
    POST   /api/llm/credentials                  (api_key mode only)
    DELETE /api/llm/credentials/{id}
    PATCH  /api/llm/credentials/{id}/activate
    POST   /api/llm/credentials/oauth/start
    POST   /api/llm/credentials/oauth/{id}/code
    GET    /api/llm/credentials/oauth/{id}/status
    POST   /api/llm/credentials/oauth/{id}/cancel
"""
import contextlib
import time
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.config import settings
from hermes.crypto import Encryptor
from hermes.errors import ErrorCode
from hermes.logging import logger
from hermes.oauth import (
    ClaudeOAuthDriver,
    OAuthDriverError,
    oauth_temp_home,
    read_credentials_raw_and_expiry,
    remove_oauth_temp_home,
)
from hermes.provider_models import (
    ModelChoice,
    ProviderModelsError,
    clear_cache,
    list_provider_models,
)
from hermes.repository import llm_credentials as repo
from hermes.repository.models import LlmCredential
from hermes.upstream import rebuild_upstream_from_db

router = APIRouter(prefix="/api/llm")


# Provider catalogue lives here, not in the schema, so we can grow it
# without a migration. Mirror the same enum the proxy resolver checks
# (haex-claude-proxy-resolver-sqlite expects these strings).
ProviderLiteral = Literal["anthropic", "openai", "openrouter", "google", "custom"]


class LlmCredentialCreate(BaseModel):
    provider: ProviderLiteral
    display_name: str = Field(min_length=1, max_length=120)
    base_url: str | None = None
    api_key: str = Field(min_length=1)


class LlmCredentialResponse(BaseModel):
    id: int
    provider: str
    mode: str
    display_name: str
    base_url: str | None
    model: str | None
    is_active: bool
    oauth_status: str | None
    oauth_authorized_at: int | None
    created_at: int
    updated_at: int


def _to_response(cred: LlmCredential) -> dict[str, Any]:
    return {
        "id": cred.id,
        "provider": cred.provider,
        "mode": cred.mode,
        "display_name": cred.display_name,
        "base_url": cred.base_url,
        "model": cred.model,
        "is_active": cred.is_active,
        "oauth_status": cred.oauth_status,
        "oauth_authorized_at": cred.oauth_authorized_at,
        "created_at": cred.created_at,
        "updated_at": cred.updated_at,
    }


@router.get("/credentials", response_model=list[LlmCredentialResponse])
async def list_credentials(request: Request) -> list[dict[str, Any]]:
    db: AsyncEngine = request.app.state.db
    rows = await repo.list_all(db)
    return [_to_response(r) for r in rows]


@router.post(
    "/credentials",
    response_model=LlmCredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential(
    request: Request, body: LlmCredentialCreate
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    encryptor: Encryptor = request.app.state.encryptor
    blob = encryptor.encrypt(body.api_key)
    cred = await repo.create_api_key(
        db,
        provider=body.provider,
        display_name=body.display_name,
        base_url=body.base_url,
        ciphertext=blob,
    )
    return _to_response(cred)


@router.delete(
    "/credentials/{cred_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_credential(request: Request, cred_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    # Null out persona.model and delete the credential in one transaction.
    # The FK cascade handles llm_credential_id=NULL; model has no cascade rule
    # so we clear it explicitly before the DELETE while we can still target by id.
    async with db.begin() as conn:
        await conn.execute(
            sql_text(
                "UPDATE personas SET model = NULL WHERE llm_credential_id = :cid"
            ),
            {"cid": cred_id},
        )
        result = await conn.execute(
            sql_text("DELETE FROM llm_credentials WHERE id = :cid"),
            {"cid": cred_id},
        )
    if (result.rowcount or 0) == 0:
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_CREDENTIAL_NOT_FOUND.value
        )
    await _refresh_upstream(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class ModelUpdateRequest(BaseModel):
    """Body for `PATCH /credentials/{id}/model`. Pass `null` to clear."""

    model: str | None = Field(default=None, max_length=200)

    @field_validator("model")
    @classmethod
    def _reject_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("model must not be empty; pass null to clear")
        return stripped


class ModelChoiceResponse(BaseModel):
    id: str
    label: str


class ModelListResponse(BaseModel):
    models: list[ModelChoiceResponse]


@router.patch(
    "/credentials/{cred_id}/model", response_model=LlmCredentialResponse
)
async def update_credential_model(
    request: Request, cred_id: int, body: ModelUpdateRequest
) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    updated = await repo.set_model(db, cred_id, body.model)
    if updated is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_CREDENTIAL_NOT_FOUND.value
        )
    # No upstream rebuild needed — the model is consulted per request
    # by the agent, the httpx client isn't keyed on the model. Drop the
    # cached provider model list so a refresh after a UI add picks up
    # newly-available models within the 10-min TTL window.
    clear_cache(cred_id)
    return _to_response(updated)


@router.get("/credentials/{cred_id}/models", response_model=ModelListResponse)
async def list_credential_models(
    request: Request, cred_id: int
) -> dict[str, Any]:
    """Resolve the provider's `/v1/models` for the given credential.

    Single-shot per (provider, cred_id): the result is cached in
    `provider_models._cache` for 10 minutes. The UI calls this when the
    user opens the model dropdown — if it 502s, the UI shows the error
    and the user can try a different credential or refresh.
    """
    db: AsyncEngine = request.app.state.db
    cred = await repo.get(db, cred_id)
    if cred is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_CREDENTIAL_NOT_FOUND.value
        )
    http: httpx.AsyncClient = request.app.state.external_http
    encryptor: Encryptor = request.app.state.encryptor
    try:
        models = await list_provider_models(cred, http=http, encryptor=encryptor)
    except ProviderModelsError as exc:
        # 502 mirrors the chat-route's "upstream unreachable" semantics;
        # 401-from-provider also bubbles up as 502 so the UI can show
        # "talk to the provider, not us".
        raise HTTPException(
            status_code=502,
            detail={
                "code": ErrorCode.LLM_PROVIDER_FAILED.value,
                "params": {"message": str(exc)},
            },
        ) from exc
    return {"models": [_choice_to_response(m) for m in models]}


def _choice_to_response(m: ModelChoice) -> dict[str, str]:
    return {"id": m.id, "label": m.label}


@router.patch(
    "/credentials/{cred_id}/activate", response_model=LlmCredentialResponse
)
async def activate_credential(request: Request, cred_id: int) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    cred = await repo.get(db, cred_id)
    if cred is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_CREDENTIAL_NOT_FOUND.value
        )
    # OAuth credentials only make sense once the code-submit step actually
    # produced a ciphertext. Activating a 'pending' / 'expired' row used to
    # leave the proxy without a usable token, surfacing as a confusing 503
    # at chat time — fail loud at the activation boundary instead.
    if cred.mode == "oauth_claude" and cred.oauth_status != "authorized":
        raise HTTPException(
            status_code=409,
            detail={
                "code": ErrorCode.LLM_OAUTH_NOT_AUTHORIZED.value,
                "params": {"state": cred.oauth_status or "pending"},
            },
        )
    if not await repo.activate(db, cred_id):
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_CREDENTIAL_NOT_FOUND.value
        )
    cred = await repo.get(db, cred_id)
    if cred is None:
        # Race with a concurrent delete — vanishingly unlikely on a
        # single-user instance, but explicit > KeyError.
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_CREDENTIAL_VANISHED.value
        )
    await _refresh_upstream(request)
    return _to_response(cred)


async def _refresh_upstream(request: Request) -> None:
    """Rebuild `app.state.upstream` so the next chat request picks up
    whatever credential is now active."""
    await rebuild_upstream_from_db(
        request.app,
        db=request.app.state.db,
        user_id=request.state.user_id,
        encryptor=request.app.state.encryptor,
        fallback_llm_url=settings.llm_url,
        fallback_llm_api_key=settings.llm_api_key,
    )


# ─── OAuth subprocess flow ────────────────────────────────────────────


class OAuthStartResponse(BaseModel):
    id: int
    url: str


class OAuthCodeRequest(BaseModel):
    code: str = Field(min_length=1)


class OAuthStatusResponse(BaseModel):
    id: int
    status: str


@router.post(
    "/credentials/oauth/start",
    response_model=OAuthStartResponse,
)
async def oauth_start(request: Request) -> dict[str, Any]:
    """Spawn `claude auth login --claudeai`, return the verification URL.

    Sweeps any existing `oauth_claude` row first so only one Claude
    identity is ever in-flight — that mirrors Specifyr's pattern and is
    the only sane shape for single-user.
    """
    db: AsyncEngine = request.app.state.db
    driver: ClaudeOAuthDriver = request.app.state.oauth_driver

    # Tear down any pre-existing oauth_claude row (cancel in-memory flow
    # + wipe tmp HOME + delete row). Pending leftovers, expired drift,
    # and authorised-but-re-auth-requested rows all get the same
    # treatment — single Anthropic identity per Hermes instance.
    rows = await repo.list_all(db)
    swept = False
    for row in rows:
        if row.mode == "oauth_claude":
            with contextlib.suppress(OAuthDriverError):
                await driver.cancel(row.id)
            remove_oauth_temp_home(row.id)
            await repo.delete(db, row.id)
            swept = True
    if swept:
        # If the swept row was the active one, the agent loop would
        # keep using a stale client until the next mutation. Force a
        # rebuild now so subsequent requests fall back correctly.
        await _refresh_upstream(request)

    cred = await repo.create_oauth_pending(db, display_name="Claude (OAuth)")
    home = oauth_temp_home(cred.id)
    try:
        url = await driver.start_login(flow_id=cred.id, home=home)
    except Exception as exc:
        logger.warning("oauth_start_failed", cred_id=cred.id, error=str(exc))
        remove_oauth_temp_home(cred.id)
        await repo.delete(db, cred.id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": ErrorCode.LLM_OAUTH_START_FAILED.value,
                "params": {"message": str(exc)},
            },
        ) from exc
    return {"id": cred.id, "url": url}


@router.post(
    "/credentials/oauth/{cred_id}/code",
    response_model=LlmCredentialResponse,
)
async def oauth_submit_code(
    request: Request, cred_id: int, body: OAuthCodeRequest
) -> dict[str, Any]:
    """Pipe the user-pasted verification code into the held subprocess
    and persist the resulting `.credentials.json` as ciphertext."""
    db: AsyncEngine = request.app.state.db
    driver: ClaudeOAuthDriver = request.app.state.oauth_driver
    encryptor: Encryptor = request.app.state.encryptor

    row = await repo.get(db, cred_id)
    if row is None or row.mode != "oauth_claude":
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_OAUTH_FLOW_NOT_FOUND.value
        )
    if row.oauth_status != "pending":
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.LLM_OAUTH_NOT_PENDING.value,
                "params": {"state": row.oauth_status or "unknown"},
            },
        )

    try:
        await driver.submit_code(cred_id, body.code)
    except OAuthDriverError as exc:
        # Bad code / CLI crash / timeout. The row stays as pending — the
        # caller can hit /oauth/start again to recycle it.
        logger.info("oauth_code_rejected", cred_id=cred_id, error=str(exc))
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.LLM_OAUTH_CODE_REJECTED.value,
                "params": {"message": str(exc)},
            },
        ) from exc

    home = oauth_temp_home(cred_id)
    creds = await read_credentials_raw_and_expiry(home)
    if creds is None:
        remove_oauth_temp_home(cred_id)
        raise HTTPException(
            status_code=500,
            detail=ErrorCode.LLM_OAUTH_NO_CREDENTIALS.value,
        )

    blob = encryptor.encrypt(creds.raw)
    updated = await repo.update_oauth_authorized(
        db,
        cred_id=cred_id,
        ciphertext=blob,
        authorized_at=int(time.time()),
    )
    remove_oauth_temp_home(cred_id)
    if updated is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_CREDENTIAL_VANISHED.value
        )
    return _to_response(updated)


@router.get(
    "/credentials/oauth/{cred_id}/status",
    response_model=OAuthStatusResponse,
)
async def oauth_status(request: Request, cred_id: int) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    row = await repo.get(db, cred_id)
    if row is None or row.mode != "oauth_claude":
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_OAUTH_FLOW_NOT_FOUND.value
        )
    return {"id": row.id, "status": row.oauth_status or "pending"}


@router.post(
    "/credentials/oauth/{cred_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def oauth_cancel(request: Request, cred_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    driver: ClaudeOAuthDriver = request.app.state.oauth_driver

    row = await repo.get(db, cred_id)
    if row is None or row.mode != "oauth_claude":
        raise HTTPException(
            status_code=404, detail=ErrorCode.LLM_OAUTH_FLOW_NOT_FOUND.value
        )
    with contextlib.suppress(OAuthDriverError):
        await driver.cancel(cred_id)
    remove_oauth_temp_home(cred_id)
    await repo.delete(db, cred_id)
    await _refresh_upstream(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
