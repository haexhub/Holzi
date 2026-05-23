"""HTTP API for `llm_credentials`.

CRUD over AES-GCM-encrypted credentials the agent loop and the haex-
claude-proxy sqlite-resolver pick up. Ciphertext columns never leak into
the API surface — `LlmCredentialResponse` is the only shape clients see.

Endpoints (all bearer-gated):
    GET    /api/llm/credentials
    POST   /api/llm/credentials                  (api_key mode only)
    DELETE /api/llm/credentials/{id}
    PATCH  /api/llm/credentials/{id}/activate

OAuth-flow endpoints land in a separate slice (phase 2 of the design doc)
so this module stays purely about the data layer.
"""
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import Encryptor
from hermes.repository import llm_credentials as repo
from hermes.repository.models import LlmCredential

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
    if not await repo.delete(db, cred_id):
        raise HTTPException(status_code=404, detail="credential not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/credentials/{cred_id}/activate", response_model=LlmCredentialResponse
)
async def activate_credential(request: Request, cred_id: int) -> dict[str, Any]:
    db: AsyncEngine = request.app.state.db
    if not await repo.activate(db, cred_id):
        raise HTTPException(status_code=404, detail="credential not found")
    cred = await repo.get(db, cred_id)
    if cred is None:
        # Race with a concurrent delete — vanishingly unlikely on a
        # single-user instance, but explicit > KeyError.
        raise HTTPException(status_code=404, detail="credential vanished")
    return _to_response(cred)
