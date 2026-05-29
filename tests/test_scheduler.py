from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import agent_tasks, conversations, messages, runs
from hermes.scheduler import AgentTaskScheduler, ConversationSweepScheduler


def _stub_runner(
    *,
    text: str = "ok",
    raises: BaseException | None = None,
) -> Callable[..., Awaitable[str]]:
    """A `run_agent`-shaped callable for scheduler tests.

    Replaces the real agent loop with a deterministic stub: appends an
    assistant message and returns the canned text, or raises before any
    persistence so the failure path can be exercised.
    """

    async def runner(
        *,
        upstream: httpx.AsyncClient,  # noqa: ARG001 — kept for signature parity
        db: AsyncEngine,
        conversation_id: int,
        system_prompt: str,  # noqa: ARG001
        model: str,  # noqa: ARG001
        tools: Any | None = None,  # noqa: ARG001
        metrics: dict[str, Any] | None = None,  # noqa: ARG001
    ) -> str:
        if raises is not None:
            raise raises
        await messages.append(
            db, conversation_id=conversation_id, role="assistant", content=text
        )
        return text

    return runner


def _scheduler(
    db: AsyncEngine,
    *,
    runner: Callable[..., Awaitable[str]] | None = None,
) -> AgentTaskScheduler:
    return AgentTaskScheduler(
        db,
        upstream_provider=lambda: None,  # type: ignore[arg-type,return-value]
        tool_factory=lambda: [],
        fallback_model="test-model",
        agent_runner=runner or _stub_runner(),
    )


# ---------------------------------------------------------------------------
# AgentTaskScheduler
# ---------------------------------------------------------------------------


async def test_fire_due_runs_one_shot_and_disables(conn: AsyncEngine) -> None:
    task = await agent_tasks.create(
        conn, title="ping", prompt="say hi", due_at=1_000, ts=500
    )

    sched = _scheduler(conn)
    fired = await sched.fire_due(now=2_000)

    assert fired == 1
    refreshed = await agent_tasks.get(conn, task.id)
    assert refreshed is not None
    assert refreshed.enabled is False  # one-shot flips off after firing
    assert refreshed.last_status == "success"
    assert refreshed.last_run_at == 2_000
    assert refreshed.last_run_id is not None

    # Run row is linked back to the task.
    run = await runs.get(conn, refreshed.last_run_id)
    assert run is not None
    assert run.agent_task_id == task.id
    assert run.channel == "task"
    assert run.status == "success"


async def test_fire_due_skips_disabled(conn: AsyncEngine) -> None:
    task = await agent_tasks.create(
        conn, title="paused", prompt="x", due_at=1_000, enabled=False, ts=500
    )

    sched = _scheduler(conn)
    fired = await sched.fire_due(now=2_000)

    assert fired == 0
    refreshed = await agent_tasks.get(conn, task.id)
    assert refreshed is not None
    assert refreshed.last_status is None


async def test_fire_due_advances_recurring(conn: AsyncEngine) -> None:
    # Daily at 08:00 UTC. ts=500 (epoch 1970-01-01) → first firing is at
    # 28800 (08:00 UTC same day). After firing once at now=30000, the next
    # due_at should be the *next* day at 08:00 UTC.
    task = await agent_tasks.create(
        conn,
        title="daily",
        prompt="run me daily",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    assert task.due_at == 28_800

    sched = _scheduler(conn)
    fired = await sched.fire_due(now=30_000)

    assert fired == 1
    refreshed = await agent_tasks.get(conn, task.id)
    assert refreshed is not None
    assert refreshed.enabled is True  # recurring stays enabled
    assert refreshed.due_at == 28_800 + 86_400  # next day same UTC hour
    assert refreshed.last_status == "success"


async def test_fire_due_records_failure_without_advancing_enabled(
    conn: AsyncEngine,
) -> None:
    task = await agent_tasks.create(
        conn, title="boom", prompt="x", due_at=1_000, ts=500
    )

    sched = _scheduler(conn, runner=_stub_runner(raises=RuntimeError("nope")))
    fired = await sched.fire_due(now=2_000)

    # Counted as fired (the loop attempted it) — `fired` counts attempts
    # that reached mark_run, regardless of success.
    assert fired == 0
    refreshed = await agent_tasks.get(conn, task.id)
    assert refreshed is not None
    # One-shot still disables itself even on failure: a perpetually broken
    # task shouldn't re-fire every tick and spam the agent_runs table.
    assert refreshed.enabled is False
    assert refreshed.last_status == "error"
    # The run row landed with status=error.
    assert refreshed.last_run_id is not None
    run = await runs.get(conn, refreshed.last_run_id)
    assert run is not None
    assert run.status == "error"


async def test_fire_due_keeps_other_tasks_running_after_one_fails(
    conn: AsyncEngine,
) -> None:
    # If one task blows up, the others scheduled at the same tick must
    # still get their chance. The scheduler can't trust user-authored
    # prompts to be well-behaved.
    bad = await agent_tasks.create(
        conn, title="bad", prompt="x", due_at=1_000, ts=500
    )
    good = await agent_tasks.create(
        conn, title="good", prompt="y", due_at=1_000, ts=500
    )

    calls: list[int] = []

    async def runner(*, db: AsyncEngine, conversation_id: int, **kwargs: Any) -> str:
        # Order matches list_due ordering (asc due_at, asc title) — `bad`
        # comes before `good`. Force `bad` to fail, `good` to succeed.
        if not calls:
            calls.append(0)
            raise RuntimeError("first one fails")
        calls.append(1)
        await messages.append(
            db, conversation_id=conversation_id, role="assistant", content="ok"
        )
        return "ok"

    sched = _scheduler(conn, runner=runner)
    await sched.fire_due(now=2_000)

    assert calls == [0, 1]
    bad_after = await agent_tasks.get(conn, bad.id)
    good_after = await agent_tasks.get(conn, good.id)
    assert bad_after is not None and bad_after.last_status == "error"
    assert good_after is not None and good_after.last_status == "success"


async def test_run_now_does_not_advance_due_at(conn: AsyncEngine) -> None:
    task = await agent_tasks.create(
        conn,
        title="daily",
        prompt="manual",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    original_due_at = task.due_at

    sched = _scheduler(conn)
    await sched.run_now(task.id)

    refreshed = await agent_tasks.get(conn, task.id)
    assert refreshed is not None
    # Manual run records the firing but leaves the next cron occurrence alone.
    assert refreshed.due_at == original_due_at
    assert refreshed.last_status == "success"
    assert refreshed.last_run_id is not None
    assert refreshed.enabled is True  # not flipped off either


async def test_run_now_missing_raises_lookup_error(conn: AsyncEngine) -> None:
    sched = _scheduler(conn)
    try:
        await sched.run_now(9999)
    except LookupError as exc:
        assert "9999" in str(exc)
    else:
        raise AssertionError("expected LookupError")


# ---------------------------------------------------------------------------
# ConversationSweepScheduler — unchanged from the old plan but kept here
# so the file's coverage matches the scheduler module surface.
# ---------------------------------------------------------------------------


async def test_conversation_sweep_deletes_expired_and_keeps_bookmarked(
    conn: AsyncEngine, tmp_path: Path
) -> None:
    expired = await conversations.create(conn, channel="web", ts=0)
    pinned = await conversations.create(
        conn, channel="web", ts=0, bookmarked=True
    )
    fresh = await conversations.create(conn, channel="web", ts=10_000_000)

    scratch_root = tmp_path / "conversations"
    scratch_root.mkdir()
    (scratch_root / str(expired.id)).mkdir()

    sweeper = ConversationSweepScheduler(conn, scratch_root)
    deleted = await sweeper.sweep(now=expired.expires_at + 1)  # type: ignore[operator]

    assert deleted == [expired.id]
    assert await conversations.get(conn, expired.id) is None
    assert await conversations.get(conn, pinned.id) is not None
    assert await conversations.get(conn, fresh.id) is not None
    assert not (scratch_root / str(expired.id)).exists()


async def test_conversation_sweep_noop_when_nothing_expired(
    conn: AsyncEngine, tmp_path: Path
) -> None:
    await conversations.create(conn, channel="web", ts=10_000_000)
    sweeper = ConversationSweepScheduler(conn, tmp_path / "conversations")
    assert await sweeper.sweep(now=10_000_001) == []
