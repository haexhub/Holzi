"""HTTP API for `messenger_accounts`.

CRUD over the messenger inboxes (signal + telegram). For signal, the
"create" path is the link-as-secondary-device flow: a /link/start call
asks signal-cli-rest-api to generate a QR PNG (returned to the
browser), then /link/poll snapshots signal-cli's registered numbers and
materialises any new one as a row in messenger_accounts. The phone
number is discovered post-scan — the user never types it.

For telegram, the user pastes a BotFather-issued token; the route calls
the Bot API's `getMe` to validate it + discover the bot's @username,
then encrypts the token with the master key and inserts a row. The
ciphertext columns (`bot_token_*`) never leave the server.

Endpoints (all bearer-gated):
    GET    /api/messenger/accounts
    POST   /api/messenger/accounts/signal/link/start   → image/png
    POST   /api/messenger/accounts/signal/link/poll
    POST   /api/messenger/accounts/telegram
    PATCH  /api/messenger/accounts/{id}/activate
    DELETE /api/messenger/accounts/{id}
"""
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import Encryptor
from hermes.logging import logger
from hermes.messenger.lifecycle import rebuild_messenger_workers_from_db
from hermes.repository import messenger as repo
from hermes.repository.models import MessengerAccount
from hermes.signal.client import list_registered_numbers, start_qr_link
from hermes.telegram.client import TelegramAuthError, fetch_bot_username

router = APIRouter(prefix="/api/messenger")


class MessengerAccountResponse(BaseModel):
    id: int
    provider: str
    is_active: bool
    phone_number: str | None
    bot_username: str | None
    allowed_chat_ids: str | None
    created_at: int
    updated_at: int


class SignalLinkPollResponse(BaseModel):
    # Accounts known to the server right now (post-poll). Frontend
    # diffs against the snapshot from before /link/start to detect the
    # freshly-linked number — but the more-recent created_at also
    # works as a tie-breaker when the user re-links the same number.
    accounts: list[MessengerAccountResponse]


class CreateTelegramRequest(BaseModel):
    # min_length=1 to reject empty strings (Pydantic accepts "" as str
    # otherwise, which would crash deep inside the Telegram client).
    bot_token: str = Field(..., min_length=1)
    # Optional allowlist of chat ids the bot may respond to. None = open
    # to any chat the bot is added to. Empty list normalised to None so
    # the worker doesn't silently drop every message.
    allowed_chat_ids: list[int] | None = None


def _to_response(account: MessengerAccount) -> dict[str, Any]:
    # Ciphertext columns (`bot_token_*`) are intentionally omitted — the
    # API surface never reveals them.
    return {
        "id": account.id,
        "provider": account.provider,
        "is_active": account.is_active,
        "phone_number": account.phone_number,
        "bot_username": account.bot_username,
        "allowed_chat_ids": account.allowed_chat_ids,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _db(request: Request) -> AsyncEngine:
    return request.app.state.db


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    accounts = await repo.list_all(_db(request))
    return {"accounts": [_to_response(a) for a in accounts]}


@router.post("/accounts/signal/link/start")
async def signal_link_start(request: Request) -> Response:
    """Trigger signal-cli's QR-link-as-secondary-device flow and stream
    the generated PNG back to the browser. Subsequent polls of
    /link/poll discover the freshly-linked phone number from
    signal-cli's account list and materialise it as a row."""
    http = getattr(request.app.state, "signal_http", None)
    if http is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="signal-cli-rest-api client not initialised — check HERMES_SIGNAL_URL",
        )
    # Unique device name per attempt so repeated link tries don't
    # confuse signal-cli's internal naming.
    device_name = f"holzi-{int(time.time())}"
    try:
        png = await start_qr_link(http, device_name=device_name)
    except Exception as e:
        logger.warning(
            "signal_qr_link_failed",
            error=str(e),
            device_name=device_name,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"signal-cli-rest-api error: {e}",
        ) from e
    return Response(
        content=png,
        media_type="image/png",
        # The browser polls /link/poll for completion; no value in caching
        # the QR itself (each attempt mints a fresh device_name anyway).
        headers={"Cache-Control": "no-store"},
    )


@router.post("/accounts/signal/link/poll")
async def signal_link_poll(request: Request) -> SignalLinkPollResponse:
    """Snapshot signal-cli's registered numbers and ensure every one of
    them has a row in messenger_accounts. New rows are created inactive
    — the frontend asks the user to confirm + activate."""
    http = getattr(request.app.state, "signal_http", None)
    if http is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="signal-cli-rest-api client not initialised",
        )
    db = _db(request)
    try:
        numbers = await list_registered_numbers(http)
    except Exception as e:
        logger.warning("signal_accounts_list_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"signal-cli-rest-api error: {e}",
        ) from e

    for number in numbers:
        existing = await repo.get_by_phone(db, number)
        if existing is None:
            await repo.create_signal(db, number)
            logger.info("signal_link_discovered", phone_number=number)

    accounts = await repo.list_all(db)
    return SignalLinkPollResponse(
        accounts=[MessengerAccountResponse(**_to_response(a)) for a in accounts]
    )


@router.post("/accounts/telegram")
async def create_telegram_account(
    body: CreateTelegramRequest, request: Request
) -> dict[str, Any]:
    """Validate the bot token against Telegram's `getMe`, then persist a
    new inactive row. The user activates it explicitly via PATCH.

    Validation here doubles as the user-feedback signal: a bad token
    short-circuits with a clean 400 instead of leaving a broken row in
    the DB."""
    encryptor: Encryptor | None = getattr(request.app.state, "encryptor", None)
    external_http = getattr(request.app.state, "external_http", None)
    if encryptor is None or external_http is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="server not initialised — try again in a moment",
        )

    try:
        bot_username = await fetch_bot_username(external_http, body.bot_token)
    except TelegramAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid bot token: {exc}",
        ) from exc
    except Exception as exc:
        logger.warning("telegram_getme_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"telegram api error: {exc}",
        ) from exc

    blob = encryptor.encrypt(body.bot_token)
    # Empty list → NULL so the worker treats it as "open to any chat".
    allowed_chat_ids_json = (
        json.dumps(body.allowed_chat_ids) if body.allowed_chat_ids else None
    )
    account = await repo.create_telegram(
        _db(request),
        bot_username=bot_username,
        bot_token_iv=blob.iv,
        bot_token_tag=blob.tag,
        bot_token_data=blob.data,
        allowed_chat_ids=allowed_chat_ids_json,
    )
    logger.info("telegram_account_created", bot_username=bot_username)
    return {"account": _to_response(account)}


@router.patch("/accounts/{account_id}/activate")
async def activate_account(account_id: int, request: Request) -> dict[str, Any]:
    db = _db(request)
    updated = await repo.activate(db, account_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    # Hot-reload both workers — per-provider rebuilds are idempotent
    # no-ops when nothing changed, so this is cheap even when only one
    # provider was touched.
    await rebuild_messenger_workers_from_db(request.app)
    return {"account": _to_response(updated)}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, request: Request) -> dict[str, Any]:
    db = _db(request)
    existing = await repo.get_by_id(db, account_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    deleted = await repo.delete(db, account_id)
    if existing.is_active:
        # Killing the active account → stop the corresponding worker.
        await rebuild_messenger_workers_from_db(request.app)
    return {"deleted": deleted}
