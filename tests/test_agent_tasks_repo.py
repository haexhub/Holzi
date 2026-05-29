import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import agent_tasks


async def test_create_one_shot(conn: AsyncEngine) -> None:
    t = await agent_tasks.create(
        conn, title="ping", prompt="hi", due_at=1_000, ts=500
    )
    assert t.due_at == 1_000
    assert t.schedule is None
    assert t.enabled is True
    assert t.last_run_at is None


async def test_create_recurring_materialises_first_firing(
    conn: AsyncEngine,
) -> None:
    t = await agent_tasks.create(
        conn,
        title="daily",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    # epoch 500 → first 08:00 UTC firing is at 28800 (08:00 same day).
    assert t.due_at == 28_800


async def test_create_rejects_both_or_neither(conn: AsyncEngine) -> None:
    with pytest.raises(ValueError):
        await agent_tasks.create(conn, title="x", prompt="x")
    with pytest.raises(ValueError):
        await agent_tasks.create(
            conn,
            title="x",
            prompt="x",
            due_at=1_000,
            schedule="0 8 * * *",
        )


async def test_create_rejects_invalid_cron(conn: AsyncEngine) -> None:
    with pytest.raises(ValueError):
        await agent_tasks.create(
            conn, title="x", prompt="x", schedule="not-a-cron"
        )


async def test_list_due_returns_only_enabled_and_due(
    conn: AsyncEngine,
) -> None:
    enabled_due = await agent_tasks.create(
        conn, title="due", prompt="x", due_at=1_000, ts=500
    )
    await agent_tasks.create(
        conn, title="future", prompt="x", due_at=5_000, ts=500
    )
    disabled = await agent_tasks.create(
        conn, title="paused", prompt="x", due_at=1_000, enabled=False, ts=500
    )

    due = await agent_tasks.list_due(conn, now=2_000)
    ids = [t.id for t in due]
    assert ids == [enabled_due.id]
    assert disabled.id not in ids


async def test_mark_run_advances_recurring_schedule(
    conn: AsyncEngine,
) -> None:
    t = await agent_tasks.create(
        conn,
        title="daily",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    assert t.due_at == 28_800

    updated = await agent_tasks.mark_run(
        conn, t.id, run_id="abc", status="success", ts=30_000
    )
    assert updated is not None
    assert updated.due_at == 28_800 + 86_400
    assert updated.enabled is True
    assert updated.last_status == "success"
    assert updated.last_run_id == "abc"


async def test_mark_run_disables_one_shot(conn: AsyncEngine) -> None:
    t = await agent_tasks.create(
        conn, title="once", prompt="x", due_at=1_000, ts=500
    )
    updated = await agent_tasks.mark_run(
        conn, t.id, run_id="abc", status="success", ts=2_000
    )
    assert updated is not None
    assert updated.enabled is False
    assert updated.due_at == 1_000  # unchanged; just disabled


async def test_update_recomputes_due_at_when_schedule_changes(
    conn: AsyncEngine,
) -> None:
    t = await agent_tasks.create(
        conn,
        title="daily",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    updated = await agent_tasks.update(
        conn, t.id, schedule="0 9 * * *", ts=500
    )
    assert updated is not None
    # The new schedule is 09:00 UTC; first firing after epoch 500 is 32400.
    assert updated.due_at == 32_400


async def test_update_can_clear_due_at_to_switch_to_recurring(
    conn: AsyncEngine,
) -> None:
    t = await agent_tasks.create(
        conn, title="t", prompt="x", due_at=1_000, ts=500
    )
    updated = await agent_tasks.update(
        conn,
        t.id,
        clear_due_at=True,
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    assert updated is not None
    assert updated.schedule == "0 8 * * *"
    assert updated.due_at == 28_800


async def test_update_rejects_clearing_both(conn: AsyncEngine) -> None:
    t = await agent_tasks.create(
        conn, title="t", prompt="x", due_at=1_000, ts=500
    )
    with pytest.raises(ValueError):
        await agent_tasks.update(
            conn, t.id, clear_due_at=True, ts=500
        )


async def test_update_rejects_recurring_to_one_shot_without_due_at(
    conn: AsyncEngine,
) -> None:
    # A naked `clear_schedule=True` on a recurring row would silently keep
    # the cached cron `due_at` as the new one-shot timestamp — almost
    # certainly not what the user meant. Require explicit due_at.
    t = await agent_tasks.create(
        conn,
        title="t",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    with pytest.raises(ValueError, match="explicit due_at"):
        await agent_tasks.update(conn, t.id, clear_schedule=True, ts=500)


async def test_update_can_switch_recurring_to_one_shot_with_explicit_due_at(
    conn: AsyncEngine,
) -> None:
    t = await agent_tasks.create(
        conn,
        title="t",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    updated = await agent_tasks.update(
        conn,
        t.id,
        clear_schedule=True,
        due_at=2_000_000_000,
        ts=500,
    )
    assert updated is not None
    assert updated.schedule is None
    assert updated.due_at == 2_000_000_000


async def test_delete_removes(conn: AsyncEngine) -> None:
    t = await agent_tasks.create(
        conn, title="t", prompt="x", due_at=1_000, ts=500
    )
    assert await agent_tasks.delete(conn, t.id) is True
    assert await agent_tasks.get(conn, t.id) is None
    assert await agent_tasks.delete(conn, t.id) is False  # idempotent miss
