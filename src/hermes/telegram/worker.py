"""Telegram long-polling worker.

Each running bot owns one TelegramWorker. The worker pumps `getUpdates`
in a loop, dispatches text messages to the same agent_runner the Signal
worker uses, and replies back to the originating chat. Per-chat
conversation threading happens via `external_id="tg:<chat_id>"` — that
keeps separate humans from being smushed into one thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.logging import logger
from hermes.repository import conversations, messages
from hermes.repository.models import Conversation
from hermes.telegram.client import TelegramClient

DEFAULT_CONVO_GAP_SECONDS = 6 * 3600
DEFAULT_POLL_TIMEOUT = 25

AgentRunner = Callable[[AsyncEngine, int], Awaitable[str]]


class TelegramWorker:
    def __init__(
        self,
        client: TelegramClient,
        db: AsyncEngine,
        *,
        agent_runner: AgentRunner,
        allowed_chat_ids: list[int] | None,
        convo_gap_seconds: int = DEFAULT_CONVO_GAP_SECONDS,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT,
    ) -> None:
        self.client = client
        self.db = db
        self.agent_runner = agent_runner
        # None = open to every chat the bot is added to. Empty list would
        # silently drop every message, which is almost never what the
        # operator wants; the API layer normalises [] → None upstream.
        self.allowed_chat_ids = allowed_chat_ids
        self.convo_gap_seconds = convo_gap_seconds
        self.poll_timeout = poll_timeout
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Telegram's getUpdates offset semantics: pass last_update_id + 1
        # to ack everything seen so far. We track the running max here.
        self._next_offset = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="telegram-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info("telegram_worker_started")
        while not self._stop.is_set():
            # Guarantee a scheduler yield even when the upstream returns
            # synchronously — keeps the loop cooperative under tests that
            # mock the http transport (which never actually suspends),
            # and is a no-op cost in production where get_updates blocks
            # for the long-poll budget.
            await asyncio.sleep(0)
            try:
                updates = await self.client.get_updates(
                    offset=self._next_offset, timeout=self.poll_timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Same backoff posture as the signal worker — keep the
                # loop alive across transient api.telegram.org hiccups.
                logger.warning("telegram_get_updates_error", error=str(exc))
                await asyncio.sleep(5)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    # Ack-by-offset semantics: the very next getUpdates
                    # call must skip this id.
                    self._next_offset = max(self._next_offset, update_id + 1)
                try:
                    await self.process_update(update)
                except Exception as exc:
                    logger.warning("telegram_process_error", error=str(exc))
        logger.info("telegram_worker_stopped")

    async def process_update(
        self, update: dict[str, Any], *, now: int | None = None
    ) -> None:
        parsed = _extract_text_message(update)
        if parsed is None:
            return
        chat_id, text = parsed

        if self.allowed_chat_ids is not None and chat_id not in self.allowed_chat_ids:
            # Silent drop — we don't even persist the message. Logging
            # would dox the chat_id into the structured-logs pipeline.
            return

        current = now if now is not None else int(time.time())
        convo = await self._resolve_conversation(chat_id, current)

        await messages.append(
            self.db,
            conversation_id=convo.id,
            role="user",
            content=text,
            ts=current,
        )
        try:
            reply = await self.agent_runner(self.db, convo.id)
            await self.client.send_message(chat_id=chat_id, text=reply)
        finally:
            # Touch even on agent/send failure — otherwise a hung agent
            # could leave the 6h gap heuristic stuck on an old timestamp
            # and split every subsequent message into a fresh thread.
            await conversations.touch(self.db, convo.id, ts=current)

    async def _resolve_conversation(self, chat_id: int, now: int) -> Conversation:
        external_id = _chat_to_external_id(chat_id)
        latest = await conversations.find_latest_by_external_id(
            self.db, channel="telegram", external_id=external_id
        )
        if latest is not None and now - latest.updated_at < self.convo_gap_seconds:
            return latest
        return await conversations.create(
            self.db, channel="telegram", external_id=external_id, ts=now
        )


def _chat_to_external_id(chat_id: int) -> str:
    return f"tg:{chat_id}"


def _extract_text_message(update: dict[str, Any]) -> tuple[int, str] | None:
    """Return `(chat_id, text)` for plain text messages, else None.

    Filters out edits, photos, stickers, channel posts — anything without
    a `message.text` field. The worker only handles direct text today;
    extending to media would require a multi-modal agent contract.
    """
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text")
    if not isinstance(text, str) or not text:
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None
    return chat_id, text
