import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import agent_tasks


async def test_create_one_shot(conn: AsyncEngine) -> None:
    t = await agent_tasks.create(
        conn, user_id=1, title="ping", prompt="hi", due_at=1_000, ts=500
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
        user_id=1,
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
        await agent_tasks.create(conn, user_id=1, title="x", prompt="x")
    with pytest.raises(ValueError):
        await agent_tasks.create(
            conn,
            user_id=1,
            title="x",
            prompt="x",
            due_at=1_000,
            schedule="0 8 * * *",
        )


async def test_create_rejects_invalid_cron(conn: AsyncEngine) -> None:
    with pytest.raises(ValueError):
        await agent_tasks.create(
            conn, user_id=1, title="x", prompt="x", schedule="not-a-cron"
        )


async def test_list_due_returns_only_enabled_and_due(
    conn: AsyncEngine,
) -> None:
    enabled_due = await agent_tasks.create(
        conn, user_id=1, title="due", prompt="x", due_at=1_000, ts=500
    )
    await agent_tasks.create(
        conn, user_id=1, title="future", prompt="x", due_at=5_000, ts=500
    )
    disabled = await agent_tasks.create(
        conn, user_id=1, title="paused", prompt="x", due_at=1_000, enabled=False, ts=500
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
        user_id=1,
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
        conn, user_id=1, title="once", prompt="x", due_at=1_000, ts=500
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
        user_id=1,
        title="daily",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    updated = await agent_tasks.update(
        conn, t.id, user_id=1, schedule="0 9 * * *", ts=500
    )
    assert updated is not None
    # The new schedule is 09:00 UTC; first firing after epoch 500 is 32400.
    assert updated.due_at == 32_400


async def test_update_can_clear_due_at_to_switch_to_recurring(
    conn: AsyncEngine,
) -> None:
    t = await agent_tasks.create(
        conn, user_id=1, title="t", prompt="x", due_at=1_000, ts=500
    )
    updated = await agent_tasks.update(
        conn,
        t.id,
        user_id=1,
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
        conn, user_id=1, title="t", prompt="x", due_at=1_000, ts=500
    )
    with pytest.raises(ValueError):
        await agent_tasks.update(
            conn, t.id, user_id=1, clear_due_at=True, ts=500
        )


async def test_update_rejects_recurring_to_one_shot_without_due_at(
    conn: AsyncEngine,
) -> None:
    # A naked `clear_schedule=True` on a recurring row would silently keep
    # the cached cron `due_at` as the new one-shot timestamp — almost
    # certainly not what the user meant. Require explicit due_at.
    t = await agent_tasks.create(
        conn,
        user_id=1,
        title="t",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    with pytest.raises(ValueError, match="explicit due_at"):
        await agent_tasks.update(conn, t.id, user_id=1, clear_schedule=True, ts=500)


async def test_update_can_switch_recurring_to_one_shot_with_explicit_due_at(
    conn: AsyncEngine,
) -> None:
    t = await agent_tasks.create(
        conn,
        user_id=1,
        title="t",
        prompt="x",
        schedule="0 8 * * *",
        timezone="UTC",
        ts=500,
    )
    updated = await agent_tasks.update(
        conn,
        t.id,
        user_id=1,
        clear_schedule=True,
        due_at=2_000_000_000,
        ts=500,
    )
    assert updated is not None
    assert updated.schedule is None
    assert updated.due_at == 2_000_000_000


async def test_delete_removes(conn: AsyncEngine) -> None:
    t = await agent_tasks.create(
        conn, user_id=1, title="t", prompt="x", due_at=1_000, ts=500
    )
    assert await agent_tasks.delete(conn, t.id, user_id=1) is True
    assert await agent_tasks.get(conn, t.id, user_id=1) is None
    assert await agent_tasks.delete(conn, t.id, user_id=1) is False  # idempotent miss


# --- Wave C1: cross-user isolation -----------------------------------------
# The `conn` fixture seeds user 1; these tests add user 2 so the
# `agent_tasks.user_id` FK holds, then assert one user can never see, update,
# or delete another's tasks. `list_due` stays GLOBAL (the single scheduler
# serves every user) — covered separately below.
async def _seed_two_users(conn: AsyncEngine) -> None:
    from sqlalchemy import text

    async with conn.begin() as db:
        await db.execute(
            text(
                "INSERT OR IGNORE INTO users(id, role, bootstrap_completed, "
                "created_at) VALUES (1,'admin',0,0)"
            )
        )
        await db.execute(
            text(
                "INSERT OR IGNORE INTO users(id, role, bootstrap_completed, "
                "created_at) VALUES (2,'member',0,0)"
            )
        )


async def test_get_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    theirs = await agent_tasks.create(
        conn, user_id=2, title="theirs", prompt="x", due_at=1_000, ts=500
    )
    # Another user's task is invisible.
    assert await agent_tasks.get(conn, theirs.id, user_id=1) is None
    assert await agent_tasks.get(conn, theirs.id, user_id=2) is not None


async def test_list_all_filters_by_user(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    mine = await agent_tasks.create(
        conn, user_id=1, title="mine", prompt="x", due_at=1_000, ts=500
    )
    await agent_tasks.create(
        conn, user_id=2, title="theirs", prompt="x", due_at=2_000, ts=500
    )
    rows = await agent_tasks.list_all(conn, user_id=1)
    assert [t.id for t in rows] == [mine.id]


async def test_update_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    theirs = await agent_tasks.create(
        conn, user_id=2, title="theirs", prompt="x", due_at=1_000, ts=500
    )
    # user 1 can't update user 2's task (no-op → None).
    assert (
        await agent_tasks.update(conn, theirs.id, user_id=1, title="hijacked", ts=500)
        is None
    )
    # Row untouched and still owned by user 2.
    still = await agent_tasks.get(conn, theirs.id, user_id=2)
    assert still is not None
    assert still.title == "theirs"


async def test_delete_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    theirs = await agent_tasks.create(
        conn, user_id=2, title="theirs", prompt="x", due_at=1_000, ts=500
    )
    # user 1 can't delete user 2's task.
    assert await agent_tasks.delete(conn, theirs.id, user_id=1) is False
    # Row untouched and still owned by user 2.
    still = await agent_tasks.get(conn, theirs.id, user_id=2)
    assert still is not None


async def test_list_due_is_global_across_users(conn: AsyncEngine) -> None:
    # `list_due` powers the single in-process scheduler, which serves every
    # user — it must return due tasks regardless of owner.
    await _seed_two_users(conn)
    mine = await agent_tasks.create(
        conn, user_id=1, title="mine", prompt="x", due_at=1_000, ts=500
    )
    theirs = await agent_tasks.create(
        conn, user_id=2, title="theirs", prompt="x", due_at=1_000, ts=500
    )
    due = await agent_tasks.list_due(conn, now=2_000)
    ids = {t.id for t in due}
    assert mine.id in ids
    assert theirs.id in ids
    # Each task carries its own user_id so the scheduler can fire under the
    # correct owner.
    owners = {t.id: t.user_id for t in due}
    assert owners[mine.id] == 1
    assert owners[theirs.id] == 2
