import asyncio
import contextlib
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.logging import logger
from hermes.repository import conversations as conversations_repo
from hermes.repository import reminders
from hermes.signal.client import SignalClient

DEFAULT_POLL_INTERVAL_SECONDS = 60
# Conversations age out in days; checking once an hour is plenty and
# keeps the loop cheap on idle deployments.
DEFAULT_CONVERSATION_SWEEP_INTERVAL_SECONDS = 3600


class ReminderScheduler:
    """In-process loop that fires due reminders.

    Currently only the `signal` channel is supported; reminders on other
    channels are logged and skipped (no auto-retry, no failure persistence).
    """

    def __init__(
        self,
        db: AsyncEngine,
        signal_client: SignalClient | None,
        signal_self_number: str | None,
        *,
        poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.db = db
        self.signal_client = signal_client
        self.self_number = signal_self_number
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="reminder-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info("reminder_scheduler_started", poll_interval=self.poll_interval)
        while not self._stop.is_set():
            try:
                await self.fire_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — don't crash the loop
                logger.warning("reminder_scheduler_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue
        logger.info("reminder_scheduler_stopped")

    async def fire_due(self, *, now: int | None = None) -> int:
        """Fire every reminder whose due_at is past. Returns count fired."""
        current = now if now is not None else int(time.time())
        due = await reminders.list_due(self.db, now=current)
        fired = 0
        for r in due:
            if r.channel != "signal":
                logger.warning(
                    "reminder_skipped_unknown_channel", id=r.id, channel=r.channel
                )
                continue
            if self.signal_client is None or not self.self_number:
                logger.warning("reminder_skipped_signal_disabled", id=r.id)
                continue
            try:
                await self.signal_client.send(
                    recipient=self.self_number, message=r.message
                )
            except Exception as exc:  # noqa: BLE001 — retry next tick
                logger.warning("reminder_send_failed", id=r.id, error=str(exc))
                continue
            await reminders.mark_fired(self.db, r.id, ts=current)
            fired += 1
        return fired


class ConversationSweepScheduler:
    """In-process loop that deletes conversations past their TTL.

    Pairs with the per-conversation scratch directory under
    `scratch_root`: every deleted row has its scratch dir removed in the
    same step. Bookmarked rows have `expires_at = NULL` and are never
    picked up by this sweep.
    """

    def __init__(
        self,
        db: AsyncEngine,
        scratch_root: Path | None,
        *,
        poll_interval: int = DEFAULT_CONVERSATION_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self.db = db
        self.scratch_root = scratch_root
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="conversation-sweep")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info(
            "conversation_sweep_started", poll_interval=self.poll_interval
        )
        while not self._stop.is_set():
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — don't crash the loop
                logger.warning("conversation_sweep_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue
        logger.info("conversation_sweep_stopped")

    async def sweep(self, *, now: int | None = None) -> list[int]:
        current = now if now is not None else int(time.time())
        deleted = await conversations_repo.sweep_expired(
            self.db, now=current, scratch_root=self.scratch_root
        )
        if deleted:
            logger.info(
                "conversation_sweep_deleted",
                count=len(deleted),
                ids=deleted,
            )
        return deleted
