from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import EncryptedBlob, Encryptor
from hermes.personas import ensure_backfill
from hermes.repository import agent_tasks, conversations, llm_credentials, messages, runs
from hermes.scheduler import AgentTaskScheduler, ConversationSweepScheduler

_DUMMY_ENCRYPTOR = Encryptor(b"\x00" * 32)


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
        user_id: int,
        conversation_id: int,
        system_prompt: str,  # noqa: ARG001
        model: str,  # noqa: ARG001
        tools: Any | None = None,  # noqa: ARG001
        metrics: dict[str, Any] | None = None,  # noqa: ARG001
    ) -> str:
        if raises is not None:
            raise raises
        await messages.append(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            role="assistant",
            content=text,
        )
        return text

    return runner


async def _scheduler(
    db: AsyncEngine,
    *,
    owner_db: AsyncEngine,
    runner: Callable[..., Awaitable[str]] | None = None,
) -> AgentTaskScheduler:
    """Seed a minimal DB (backfill + active credential) and return a scheduler."""
    await ensure_backfill(db, user_id=1)
    cred = await llm_credentials.create_api_key(
        db,
        user_id=1,
        provider="anthropic",
        display_name="test-cred",
        base_url=None,
        ciphertext=EncryptedBlob(iv="aa", tag="bb", data="cc"),
    )
    await llm_credentials.activate(db, cred.id, user_id=1)
    return AgentTaskScheduler(
        db,
        owner_db=owner_db,
        encryptor=_DUMMY_ENCRYPTOR,
        fallback_proxy_url="http://proxy.test",
        tool_factory=lambda: [],
        fallback_model="test-model",
        agent_runner=runner or _stub_runner(),
    )


# ---------------------------------------------------------------------------
# AgentTaskScheduler
# ---------------------------------------------------------------------------


async def test_fire_due_runs_one_shot_and_disables(
    conn: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    task = await agent_tasks.create(
        conn, user_id=1, title="ping", prompt="say hi", due_at=1_000, ts=500
    )

    sched = await _scheduler(conn, owner_db=owner_engine)
    fired = await sched.fire_due(now=2_000)

    assert fired == 1
    refreshed = await agent_tasks.get(conn, task.id, user_id=1)
    assert refreshed is not None
    assert refreshed.enabled is False  # one-shot flips off after firing
    assert refreshed.last_status == "success"
    assert refreshed.last_run_at == 2_000
    assert refreshed.last_run_id is not None

    # Run row is linked back to the task.
    run = await runs.get(conn, refreshed.last_run_id, user_id=1)
    assert run is not None
    assert run.agent_task_id == task.id
    assert run.channel == "task"
    assert run.status == "success"


async def test_fire_due_passes_composed_persona_channel_system_prompt(
    conn: AsyncEngine, owner_engine: AsyncEngine,
) -> None:
    """Plan 29-A: the scheduler resolves `get_effective_system_prompt(
    "task", db)` and feeds the composition to `run_agent`. Two states:
    (a) backfill only → default Hermes + default task prompt;
    (b) custom channel prompt → composition uses the override."""
    import time

    from sqlalchemy import text

    from hermes.personas import (
        CHANNEL_REGISTRY,
        DEFAULT_PERSONA_AGENTS,
        DEFAULT_PERSONA_IDENTITY,
        DEFAULT_PERSONA_SOUL,
        ensure_backfill,
    )
    from hermes.repository import channels as channels_repo

    await ensure_backfill(conn, user_id=1)
    # User 1 already exists (seeded by `conn`); flip bootstrap_completed=true
    # so the bootstrap hint is suppressed — this test pins the exact
    # system-prompt composition and doesn't care about onboarding.
    async with conn.begin() as txn:
        await txn.execute(
            text(
                "UPDATE users SET bootstrap_completed=true, created_at=:ts WHERE id=1"
            ),
            {"ts": int(time.time())},
        )

    captured: list[str] = []

    async def capturing_runner(
        *,
        upstream: httpx.AsyncClient,  # noqa: ARG001
        db: AsyncEngine,  # noqa: ARG001
        user_id: int,  # noqa: ARG001
        conversation_id: int,  # noqa: ARG001
        system_prompt: str,
        model: str,  # noqa: ARG001
        tools: Any | None = None,  # noqa: ARG001
        metrics: dict[str, Any] | None = None,  # noqa: ARG001
    ) -> str:
        captured.append(system_prompt)
        return "ok"

    # Plan 36 default seed has all three fragments populated, so the
    # composed prompt opens with Soul → Identity → Agents sections.
    default_persona_block = (
        f"## Soul\n{DEFAULT_PERSONA_SOUL}\n\n"
        f"## Identity\n{DEFAULT_PERSONA_IDENTITY}\n\n"
        f"## Agents\n{DEFAULT_PERSONA_AGENTS}"
    )

    # (a) default backfill composition.
    await agent_tasks.create(
        conn, user_id=1, title="t1", prompt="run me", due_at=1_000, ts=500
    )
    sched = await _scheduler(conn, owner_db=owner_engine, runner=capturing_runner)
    await sched.fire_due(now=2_000)
    assert captured[-1] == (
        f"{default_persona_block}\n\n"
        f"{CHANNEL_REGISTRY['task']['default_prompt']}"
    )

    # (b) custom channel prompt → flows through.
    await channels_repo.update(conn, "task", prompt="Custom task prompt.")
    await agent_tasks.create(
        conn, user_id=1, title="t2", prompt="run me too", due_at=1_000, ts=500
    )
    await sched.fire_due(now=3_000)
    assert captured[-1] == (
        f"{default_persona_block}\n\nCustom task prompt."
    )


async def test_fire_due_creates_conversation_owned_by_task_owner(
    conn: AsyncEngine, owner_engine: AsyncEngine,
) -> None:
    # Wave C1: the scheduler is global (one loop serves every user), but each
    # fired task's conversation must be owned by the task's own user_id — not
    # hardcoded to the admin. Seed user 2 + their task and assert the resulting
    # conversation belongs to user 2.
    async with conn.begin() as txn:
        await txn.execute(
            text(
                "INSERT INTO users(id, role, bootstrap_completed, "
                "created_at) VALUES (2,'member',false,0) ON CONFLICT (id) DO NOTHING"
            )
        )
    task = await agent_tasks.create(
        conn, user_id=2, title="theirs", prompt="run me", due_at=1_000, ts=500
    )
    # The scheduler resolves the persona context under the TASK's owner, so
    # user 2 needs their own active credential or the fire raises 503.
    cred2 = await llm_credentials.create_api_key(
        conn,
        user_id=2,
        provider="anthropic",
        display_name="u2-cred",
        base_url=None,
        ciphertext=EncryptedBlob(iv="aa", tag="bb", data="cc"),
    )
    await llm_credentials.activate(conn, cred2.id, user_id=2)

    captured: list[int] = []

    async def capturing_runner(
        *,
        upstream: httpx.AsyncClient,  # noqa: ARG001
        db: AsyncEngine,
        user_id: int,
        conversation_id: int,
        system_prompt: str,  # noqa: ARG001
        model: str,  # noqa: ARG001
        tools: Any | None = None,  # noqa: ARG001
        metrics: dict[str, Any] | None = None,  # noqa: ARG001
    ) -> str:
        captured.append(conversation_id)
        await messages.append(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            role="assistant",
            content="ok",
        )
        return "ok"

    sched = await _scheduler(conn, owner_db=owner_engine, runner=capturing_runner)
    fired = await sched.fire_due(now=2_000)

    assert fired == 1
    assert len(captured) == 1
    convo_id = captured[0]
    # The conversation is owned by user 2 (the task's owner) — visible to
    # user 2 and invisible to the admin.
    assert await conversations.get(conn, convo_id, user_id=2) is not None
    assert await conversations.get(conn, convo_id, user_id=1) is None
    # Sanity: the task itself is still owned by user 2.
    refreshed = await agent_tasks.get(conn, task.id, user_id=2)
    assert refreshed is not None
    assert refreshed.user_id == 2


async def test_fire_due_skips_disabled(conn: AsyncEngine, owner_engine: AsyncEngine) -> None:
    task = await agent_tasks.create(
        conn, user_id=1, title="paused", prompt="x", due_at=1_000, enabled=False, ts=500
    )

    sched = await _scheduler(conn, owner_db=owner_engine)
    fired = await sched.fire_due(now=2_000)

    assert fired == 0
    refreshed = await agent_tasks.get(conn, task.id, user_id=1)
    assert refreshed is not None
    assert refreshed.last_status is None


async def test_fire_due_advances_recurring(conn: AsyncEngine, owner_engine: AsyncEngine) -> None:
    # Daily at 08:00 UTC. ts=500 (epoch 1970-01-01) → first firing is at
    # 28800 (08:00 UTC same day). After firing once at now=30000, the next
    # due_at should be the *next* day at 08:00 UTC.
    task = await agent_tasks.create(
        conn,
        user_id=1,
        title="daily",
        prompt="run me daily",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    assert task.due_at == 28_800

    sched = await _scheduler(conn, owner_db=owner_engine)
    fired = await sched.fire_due(now=30_000)

    assert fired == 1
    refreshed = await agent_tasks.get(conn, task.id, user_id=1)
    assert refreshed is not None
    assert refreshed.enabled is True  # recurring stays enabled
    assert refreshed.due_at == 28_800 + 86_400  # next day same UTC hour
    assert refreshed.last_status == "success"


async def test_fire_due_records_failure_without_advancing_enabled(
    conn: AsyncEngine, owner_engine: AsyncEngine,
) -> None:
    task = await agent_tasks.create(
        conn, user_id=1, title="boom", prompt="x", due_at=1_000, ts=500
    )

    sched = await _scheduler(
        conn, owner_db=owner_engine, runner=_stub_runner(raises=RuntimeError("nope"))
    )
    fired = await sched.fire_due(now=2_000)

    # `fired` counts only successful firings (the `fired += 1` lives after
    # `_fire_one`, which re-raises on the runner's error). The failure is
    # still recorded on the row via `mark_run` in the finally block.
    assert fired == 0
    refreshed = await agent_tasks.get(conn, task.id, user_id=1)
    assert refreshed is not None
    # One-shot still disables itself even on failure: a perpetually broken
    # task shouldn't re-fire every tick and spam the agent_runs table.
    assert refreshed.enabled is False
    assert refreshed.last_status == "error"
    # The run row landed with status=error.
    assert refreshed.last_run_id is not None
    run = await runs.get(conn, refreshed.last_run_id, user_id=1)
    assert run is not None
    assert run.status == "error"


async def test_fire_due_keeps_other_tasks_running_after_one_fails(
    conn: AsyncEngine, owner_engine: AsyncEngine,
) -> None:
    # If one task blows up, the others scheduled at the same tick must
    # still get their chance. The scheduler can't trust user-authored
    # prompts to be well-behaved. Dispatch on the conversation title
    # rather than call order: list_due only orders by due_at, so two rows
    # with the same due_at could come back in implementation-defined order.
    bad = await agent_tasks.create(
        conn, user_id=1, title="bad", prompt="x", due_at=1_000, ts=500
    )
    good = await agent_tasks.create(
        conn, user_id=1, title="good", prompt="y", due_at=1_000, ts=500
    )

    async def runner(
        *, db: AsyncEngine, user_id: int, conversation_id: int, **kwargs: Any
    ) -> str:
        convo = await conversations.get(db, conversation_id, user_id=user_id)
        assert convo is not None
        if convo.title == "[task] bad":
            raise RuntimeError("this one fails")
        await messages.append(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            role="assistant",
            content="ok",
        )
        return "ok"

    sched = await _scheduler(conn, owner_db=owner_engine, runner=runner)
    await sched.fire_due(now=2_000)

    bad_after = await agent_tasks.get(conn, bad.id, user_id=1)
    good_after = await agent_tasks.get(conn, good.id, user_id=1)
    assert bad_after is not None and bad_after.last_status == "error"
    assert good_after is not None and good_after.last_status == "success"


async def test_run_now_does_not_advance_due_at(
    conn: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    task = await agent_tasks.create(
        conn,
        user_id=1,
        title="daily",
        prompt="manual",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    original_due_at = task.due_at

    sched = await _scheduler(conn, owner_db=owner_engine)
    await sched.run_now(task.id)

    refreshed = await agent_tasks.get(conn, task.id, user_id=1)
    assert refreshed is not None
    # Manual run records the firing but leaves the next cron occurrence alone.
    assert refreshed.due_at == original_due_at
    assert refreshed.last_status == "success"
    assert refreshed.last_run_id is not None
    assert refreshed.enabled is True  # not flipped off either


async def test_run_now_does_not_disable_one_shot(
    conn: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    # The other half of advance=False: a manual run of a one-shot must NOT
    # flip enabled=0 — that disable belongs to the real scheduled firing.
    task = await agent_tasks.create(
        conn, user_id=1, title="once", prompt="manual", due_at=2_000_000_000, ts=500
    )

    sched = await _scheduler(conn, owner_db=owner_engine)
    await sched.run_now(task.id)

    refreshed = await agent_tasks.get(conn, task.id, user_id=1)
    assert refreshed is not None
    assert refreshed.enabled is True  # still due for the real firing
    assert refreshed.due_at == 2_000_000_000
    assert refreshed.last_status == "success"


async def test_run_now_missing_raises_lookup_error(
    conn: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    sched = await _scheduler(conn, owner_db=owner_engine)
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
    conn: AsyncEngine, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    expired = await conversations.create(conn, user_id=1, channel="web", ts=0)
    pinned = await conversations.create(
        conn, user_id=1, channel="web", ts=0, bookmarked=True
    )
    fresh = await conversations.create(conn, user_id=1, channel="web", ts=10_000_000)

    scratch_root = tmp_path / "conversations"
    scratch_root.mkdir()
    (scratch_root / str(expired.id)).mkdir()

    sweeper = ConversationSweepScheduler(conn, scratch_root, owner_db=owner_engine)
    deleted = await sweeper.sweep(now=expired.expires_at + 1)  # type: ignore[operator]

    assert deleted == [expired.id]
    assert await conversations.get(conn, expired.id, user_id=1) is None
    assert await conversations.get(conn, pinned.id, user_id=1) is not None
    assert await conversations.get(conn, fresh.id, user_id=1) is not None
    assert not (scratch_root / str(expired.id)).exists()


async def test_conversation_sweep_noop_when_nothing_expired(
    conn: AsyncEngine, owner_engine: AsyncEngine, tmp_path: Path
) -> None:
    await conversations.create(conn, user_id=1, channel="web", ts=10_000_000)
    sweeper = ConversationSweepScheduler(conn, tmp_path / "conversations", owner_db=owner_engine)
    assert await sweeper.sweep(now=10_000_001) == []
