import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import run_agent
from hermes.logging import logger
from hermes.repository import agent_tasks as agent_tasks_repo
from hermes.repository import conversations as conversations_repo
from hermes.repository import llm_credentials as llm_credentials_repo
from hermes.repository import messages as messages_repo
from hermes.run_tracker import track_run

DEFAULT_POLL_INTERVAL_SECONDS = 60
# Conversations age out in days; checking once an hour is plenty and
# keeps the loop cheap on idle deployments.
DEFAULT_CONVERSATION_SWEEP_INTERVAL_SECONDS = 3600

# Channel the scheduler tags its auto-created conversations with. Distinct
# from "web" so the right-rail conversation list can filter task threads out
# (or feature them separately) — the agent itself sees the same web-style
# system prompt regardless.
TASK_CHANNEL = "task"

TASK_SYSTEM_PROMPT = (
    "You are Hermes running a scheduled agent task on behalf of Martin. "
    "Execute the user prompt below; be concise. Use tools when needed."
)


# A factory the scheduler calls per-run to obtain the upstream httpx client.
# Mirrors how routes/api.py reads `app.state.upstream`: it can be rebuilt at
# runtime when credentials change, so caching the client at construction
# time would pin a stale handle.
UpstreamProvider = Callable[[], httpx.AsyncClient]
ToolFactory = Callable[[], list]


class AgentTaskScheduler:
    """In-process loop that fires due agent tasks (Plan 16).

    Replaces the old reminder scheduler. On each tick:
    1. Fetch enabled rows whose `due_at <= now`.
    2. For each: create a fresh conversation (channel=`task`), append the
       task's prompt as the user message, then run the agent through
       `run_agent` with the same web tool catalog. Wrap the call in
       `track_run` so the row in `agent_runs` carries `agent_task_id`.
    3. After the run (success or error) call `agent_tasks.mark_run` —
       which advances `due_at` on recurring rows and flips `enabled=0` on
       one-shot rows.

    The scheduler lives in the main worker, not in any sandbox, so a
    misbehaving task can't kill the tick loop (per Plan 16 + Plan 11b).
    Tool calls inside `run_agent` route through SandboxManager exactly the
    same way `/api/chat` does — no special path here.
    """

    def __init__(
        self,
        db: AsyncEngine,
        *,
        upstream_provider: UpstreamProvider,
        tool_factory: ToolFactory,
        fallback_model: str,
        poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
        # Test hook: lets unit tests stub out the agent invocation so they
        # can assert scheduling behaviour without booting a real upstream.
        agent_runner: Callable[..., Awaitable[str]] | None = None,
    ) -> None:
        self.db = db
        self._upstream_provider = upstream_provider
        self._tool_factory = tool_factory
        self._fallback_model = fallback_model
        self.poll_interval = poll_interval
        self._agent_runner = agent_runner or run_agent
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="agent-task-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info("agent_task_scheduler_started", poll_interval=self.poll_interval)
        while not self._stop.is_set():
            try:
                await self.fire_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — don't crash the loop
                logger.warning("agent_task_scheduler_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except TimeoutError:
                continue
        logger.info("agent_task_scheduler_stopped")

    async def fire_due(self, *, now: int | None = None) -> int:
        """Fire every task whose `due_at` is past. Returns count fired."""
        current = now if now is not None else int(time.time())
        due = await agent_tasks_repo.list_due(self.db, now=current)
        fired = 0
        for task in due:
            try:
                await self._fire_one(task, now=current)
                fired += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep firing other rows
                logger.warning(
                    "agent_task_fire_failed",
                    task_id=task.id,
                    error=str(exc),
                )
        return fired

    async def run_now(self, task_id: int) -> str:
        """Fire a single task immediately, regardless of its `due_at`/`enabled`.

        Used by the POST /api/tasks/{id}/run endpoint and the `task_run` tool
        the agent can call from chat. Returns the agent's final assistant
        text. Does NOT advance the cron `due_at` — a manual run shouldn't
        skip the next scheduled occurrence (mark_run still records
        last_run_at/status for visibility).
        """
        task = await agent_tasks_repo.get(self.db, task_id)
        if task is None:
            raise LookupError(f"agent_task {task_id} not found")
        return await self._fire_one(task, now=int(time.time()), advance_due_at=False)

    async def _fire_one(
        self, task, *, now: int, advance_due_at: bool = True
    ) -> str:
        # One conversation per firing keeps each task's output independently
        # browsable in the conversations list (and ages out via the normal
        # TTL sweep). Title prefixed with the task title for findability.
        convo = await conversations_repo.create(
            self.db,
            channel=TASK_CHANNEL,
            title=f"[task] {task.title}",
            ts=now,
        )
        await messages_repo.append(
            self.db,
            conversation_id=convo.id,
            role="user",
            content=task.prompt,
        )

        model = (
            await llm_credentials_repo.get_active_model(self.db)
        ) or self._fallback_model
        run_id = uuid.uuid4().hex
        metrics: dict[str, Any] = {}

        run_status = "error"
        try:
            async with track_run(
                self.db,
                run_id=run_id,
                conversation_id=convo.id,
                channel=TASK_CHANNEL,
                model=model,
                metrics=metrics,
                agent_task_id=task.id,
            ):
                result = await self._agent_runner(
                    upstream=self._upstream_provider(),
                    db=self.db,
                    conversation_id=convo.id,
                    system_prompt=TASK_SYSTEM_PROMPT,
                    model=model,
                    tools=self._tool_factory(),
                    metrics=metrics,
                )
                run_status = "success"
                return result
        finally:
            # Always record the firing, even on error: that's what last_status
            # is for. mark_run also advances due_at / disables one-shot rows.
            try:
                if advance_due_at:
                    await agent_tasks_repo.mark_run(
                        self.db,
                        task.id,
                        run_id=run_id,
                        status=run_status,
                        ts=now,
                    )
                else:
                    # Manual run — only update last_*; the scheduler will
                    # re-pick up the next regular firing as normal.
                    await self._update_last_run_only(
                        task.id, run_id=run_id, status=run_status, ts=now
                    )
            except Exception:  # noqa: BLE001 — never crash the tick loop
                logger.exception("agent_task_mark_run_failed", task_id=task.id)

    async def _update_last_run_only(
        self, task_id: int, *, run_id: str, status: str, ts: int
    ) -> None:
        """Write last_run_* without touching `due_at`/`enabled`. Used by
        `run_now` so an ad-hoc manual run doesn't advance the cron schedule
        or disable a one-shot before its scheduled firing.
        """
        from hermes.schema import agent_tasks as t_agent_tasks

        async with self.db.begin() as conn:
            await conn.execute(
                t_agent_tasks.update()
                .where(t_agent_tasks.c.id == task_id)
                .values(
                    last_run_at=ts,
                    last_status=status,
                    last_run_id=run_id,
                    updated_at=ts,
                )
            )


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
