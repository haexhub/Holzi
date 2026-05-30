"""Repository-level tests for `sandbox_crashes` (Plan 20-A).

Drives `insert` + `list_recent` directly against the engine fixture so
the endpoint and main.py wiring can rely on a known-good persistence
layer.
"""
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import sandbox_crashes as repo


async def test_insert_returns_row_id_and_roundtrips(conn: AsyncEngine) -> None:
    row_id = await repo.insert(
        conn,
        workspace_id="ws-1",
        sandbox_id="cont-abc",
        crashed_at=1_000,
        state="crashed",
        exit_code=137,
    )
    assert row_id > 0

    rows = await repo.list_recent(conn)
    assert len(rows) == 1
    only = rows[0]
    assert only.id == row_id
    assert only.workspace_id == "ws-1"
    assert only.sandbox_id == "cont-abc"
    assert only.crashed_at == 1_000
    assert only.state == "crashed"
    assert only.exit_code == 137
    # Reserved for a future follow-up; today the handler never sets it.
    assert only.last_message is None


async def test_insert_persists_last_message_when_supplied(conn: AsyncEngine) -> None:
    await repo.insert(
        conn,
        workspace_id="ws-1",
        sandbox_id="cont-abc",
        crashed_at=1_000,
        state="oom",
        exit_code=None,
        last_message="killed by OOM",
    )
    rows = await repo.list_recent(conn)
    assert rows[0].last_message == "killed by OOM"
    # OOM transitions carry no clean exit code — None must round-trip.
    assert rows[0].exit_code is None
    assert rows[0].state == "oom"


async def test_list_recent_orders_newest_first(conn: AsyncEngine) -> None:
    await repo.insert(
        conn,
        workspace_id="ws-1",
        sandbox_id="cont-old",
        crashed_at=1_000,
        state="crashed",
        exit_code=1,
    )
    await repo.insert(
        conn,
        workspace_id="ws-1",
        sandbox_id="cont-mid",
        crashed_at=2_000,
        state="crashed",
        exit_code=1,
    )
    await repo.insert(
        conn,
        workspace_id="ws-2",
        sandbox_id="cont-new",
        crashed_at=3_000,
        state="oom",
        exit_code=None,
    )

    rows = await repo.list_recent(conn)
    assert [r.sandbox_id for r in rows] == ["cont-new", "cont-mid", "cont-old"]


async def test_list_recent_respects_limit(conn: AsyncEngine) -> None:
    for ts in range(5):
        await repo.insert(
            conn,
            workspace_id="ws-1",
            sandbox_id=f"cont-{ts}",
            crashed_at=ts,
            state="crashed",
            exit_code=1,
        )

    rows = await repo.list_recent(conn, limit=2)
    assert len(rows) == 2
    # Newest first — the two highest crashed_at values.
    assert rows[0].sandbox_id == "cont-4"
    assert rows[1].sandbox_id == "cont-3"


async def test_list_recent_with_non_positive_limit_returns_empty(
    conn: AsyncEngine,
) -> None:
    await repo.insert(
        conn,
        workspace_id="ws-1",
        sandbox_id="cont-abc",
        crashed_at=1_000,
        state="crashed",
        exit_code=1,
    )
    assert await repo.list_recent(conn, limit=0) == []
    assert await repo.list_recent(conn, limit=-1) == []


async def test_list_recent_empty(conn: AsyncEngine) -> None:
    assert await repo.list_recent(conn) == []
