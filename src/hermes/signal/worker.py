import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.logging import logger
from hermes.repository import conversations, messages
from hermes.repository.models import Conversation
from hermes.signal.client import SignalClient

# Two unrelated boundaries used to share a name in the poll era. After Plan 28
# only the conversation-gap heuristic survives — it's about session boundaries,
# not network polling.
CONVO_GAP_SECONDS = 6 * 3600

# Reconnect backoff for the WebSocket receive stream. Step values, not strict
# doubling — picked to fail fast while still capping at 30s under sustained
# outages (e.g. signal-cli-rest-api image swap via AutoUpdate).
_BACKOFF_STEPS_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)

# Module-level alias so tests can patch sleep without touching the global
# asyncio module.
_sleep = asyncio.sleep

AgentRunner = Callable[[AsyncEngine, int], Awaitable[str]]


class SignalWorker:
    def __init__(
        self,
        client: SignalClient,
        db: AsyncEngine,
        self_number: str,
        *,
        agent_runner: AgentRunner,
    ) -> None:
        self.client = client
        self.db = db
        self.self_number = self_number
        self.agent_runner = agent_runner
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="signal-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info("signal_worker_started", number=self.self_number)
        backoff_idx = 0
        while not self._stop.is_set():
            try:
                async for envelope in self.client.receive_stream():
                    backoff_idx = 0  # reset on each successful frame
                    try:
                        await self.process_envelope(envelope)
                    except Exception as exc:
                        logger.warning("signal_process_error", error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # ConnectionClosed is the expected case (signal-cli-rest-api
                # restarts, network blips); anything else also recovers via
                # reconnect rather than tearing down the worker.
                logger.warning(
                    "signal_ws_disconnected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    backoff_seconds=_BACKOFF_STEPS_SECONDS[backoff_idx],
                )
                await _sleep(_BACKOFF_STEPS_SECONDS[backoff_idx])
                backoff_idx = min(backoff_idx + 1, len(_BACKOFF_STEPS_SECONDS) - 1)
        logger.info("signal_worker_stopped")

    async def process_envelope(self, envelope: dict[str, Any], *, now: int | None = None) -> None:
        text = _extract_note_to_self_text(envelope, self.self_number)
        if text is None:
            return

        current = now if now is not None else int(time.time())
        convo = await self._resolve_conversation(current)

        await messages.append(
            self.db, conversation_id=convo.id, role="user", content=text, ts=current
        )
        try:
            # Agent persists the assistant turn (and any tool turns) itself.
            reply = await self.agent_runner(self.db, convo.id)
            await self.client.send(recipient=self.self_number, message=reply)
        finally:
            # Touch even if agent_runner or send fails, otherwise the 6h gap
            # heuristic in _resolve_conversation can pick a stale conversation
            # on the next inbound message.
            await conversations.touch(self.db, convo.id, ts=current)

    async def _resolve_conversation(self, now: int) -> Conversation:
        latest = await conversations.list_by_channel(self.db, "signal", limit=1)
        if latest and now - latest[0].updated_at < CONVO_GAP_SECONDS:
            return latest[0]
        return await conversations.create(self.db, channel="signal", ts=now)


def _extract_note_to_self_text(envelope: dict[str, Any], self_number: str) -> str | None:
    inner = envelope.get("envelope") or {}
    if inner.get("source") != self_number:
        return None
    data = inner.get("dataMessage") or {}
    text = data.get("message")
    if not isinstance(text, str) or not text:
        return None
    return text
