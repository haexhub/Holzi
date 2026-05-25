"""Hot-reload of the Telegram worker based on the active messenger account.

Mirrors `signal/lifecycle.py` — same compare-then-rebuild pattern, but
keyed on `(bot_username, allowed_chat_ids)` since a Telegram bot token
itself is opaque (only `bot_username` is stored plaintext, the token is
AES-GCM ciphertext that needs decrypt before passing to the client).

Worker state lives on `app.state.telegram_*`:
    telegram_bot_username        string of the bot the current worker polls,
                                  or None when no account is active
    telegram_allowed_chat_ids    list[int] or None — the allowlist the
                                  current worker was started with
    telegram_worker              running TelegramWorker, or None

The TelegramClient reuses `app.state.external_http` for outbound calls —
httpx clients are safe to share across concurrent tasks, and that keeps
the test-mocking surface to one place.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import EncryptedBlob, Encryptor
from hermes.logging import logger
from hermes.repository import messenger as repo
from hermes.repository.models import MessengerAccount
from hermes.telegram.client import TelegramClient
from hermes.telegram.worker import TelegramWorker

AgentRunnerFactory = Callable[[AsyncEngine, int], Awaitable[str]]


async def rebuild_telegram_worker_from_db(app: FastAPI) -> None:
    """Reconcile the running Telegram worker with the active row in
    `messenger_accounts` (provider='telegram'). Idempotent.

    Restarts the worker when either the bot identity or the allowlist
    changes — we re-check both rather than just bot_username because the
    allowlist is also load-bearing for routing decisions inside the
    worker.
    """
    db: AsyncEngine | None = getattr(app.state, "db", None)
    if db is None:
        return  # lifespan hasn't initialised the engine yet
    encryptor: Encryptor | None = getattr(app.state, "encryptor", None)
    if encryptor is None:
        return  # ditto for the master-key resolver
    external_http: httpx.AsyncClient | None = getattr(
        app.state, "external_http", None
    )
    if external_http is None:
        # Without the shared outbound client we can't reach the Bot API.
        # Tests that mount the router without lifespan land here and
        # silently skip — same defensive shape as signal/lifecycle.py.
        return

    active = await repo.get_active(db, "telegram")
    current_username: str | None = getattr(app.state, "telegram_bot_username", None)
    current_allowlist: list[int] | None = getattr(
        app.state, "telegram_allowed_chat_ids", None
    )

    desired_username = active.bot_username if active else None
    desired_allowlist = (
        _parse_allowed_chat_ids(active.allowed_chat_ids) if active else None
    )

    if (
        desired_username == current_username
        and desired_allowlist == current_allowlist
    ):
        return

    await _stop_current_worker(app)

    if active is None or desired_username is None:
        return  # nothing active → stay stopped

    agent_runner_factory: AgentRunnerFactory | None = getattr(
        app.state, "telegram_agent_runner_factory", None
    )
    if agent_runner_factory is None:
        logger.error("telegram_worker_no_agent_runner")
        return

    try:
        bot_token = _decrypt_bot_token(encryptor, active)
    except Exception as exc:
        # If decrypt fails the row is unusable — log loud and leave the
        # worker stopped so a fresh token can be inserted via the UI.
        logger.error("telegram_token_decrypt_failed", error=str(exc))
        return

    client = TelegramClient(external_http, bot_token)
    worker = TelegramWorker(
        client,
        db,
        agent_runner=agent_runner_factory,
        allowed_chat_ids=desired_allowlist,
    )
    await worker.start()
    app.state.telegram_worker = worker
    app.state.telegram_bot_username = desired_username
    app.state.telegram_allowed_chat_ids = desired_allowlist
    logger.info("telegram_worker_started", bot_username=desired_username)


async def _stop_current_worker(app: FastAPI) -> None:
    worker = getattr(app.state, "telegram_worker", None)
    if worker is not None:
        await worker.stop()
        logger.info(
            "telegram_worker_stopped",
            bot_username=getattr(app.state, "telegram_bot_username", None),
        )
    app.state.telegram_worker = None
    app.state.telegram_bot_username = None
    app.state.telegram_allowed_chat_ids = None


def _decrypt_bot_token(encryptor: Encryptor, account: MessengerAccount) -> str:
    if (
        not account.bot_token_iv
        or not account.bot_token_tag
        or not account.bot_token_data
    ):
        raise ValueError("telegram account has no bot_token ciphertext")
    return encryptor.decrypt(
        EncryptedBlob(
            iv=account.bot_token_iv,
            tag=account.bot_token_tag,
            data=account.bot_token_data,
        )
    )


def _parse_allowed_chat_ids(raw: str | None) -> list[int] | None:
    """`allowed_chat_ids` column carries a JSON array of stringified ints
    (matches the API contract); convert to a list[int] for the worker.
    NULL or empty → None so the worker treats it as "open to any chat"."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("telegram_allowed_chat_ids_unparsable", raw=raw)
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    return [int(x) for x in parsed]
