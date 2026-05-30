"""Persistence layer for the `sandbox_crashes` table (Plan 20-A).

The health watcher in `SandboxManager` (Plan 11b-b) emits a
`WorkspaceCrash` exactly once per dead-transition. A crash handler
registered in `main.py`'s lifespan writes one row here so that a crash
that happens while no chat stream is connected still surfaces on
/settings/diagnostics after the fact — and survives an agent-container
restart.

Read-only from the API side: the only writer is the lifespan-registered
persistence handler. `list_recent` powers the "Sandbox-Abstürze" panel.
"""
from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.schema import sandbox_crashes as t_sandbox_crashes


@dataclass(frozen=True, slots=True)
class SandboxCrashRecord:
    """A row from `sandbox_crashes`. `state` is the value of the
    `SandboxState` enum the watcher saw — kept as a string here so the
    repository doesn't pull the sandbox package into its import graph."""

    id: int
    workspace_id: str
    sandbox_id: str
    crashed_at: int
    state: str
    exit_code: int | None
    last_message: str | None


def _row_to_record(row) -> SandboxCrashRecord:
    return SandboxCrashRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        sandbox_id=row.sandbox_id,
        crashed_at=row.crashed_at,
        state=row.state,
        exit_code=row.exit_code,
        last_message=row.last_message,
    )


async def insert(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    sandbox_id: str,
    crashed_at: int,
    state: str,
    exit_code: int | None,
    last_message: str | None = None,
) -> int:
    """Append one crash record. Returns the new row id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            t_sandbox_crashes.insert()
            .values(
                workspace_id=workspace_id,
                sandbox_id=sandbox_id,
                crashed_at=crashed_at,
                state=state,
                exit_code=exit_code,
                last_message=last_message,
            )
            .returning(t_sandbox_crashes.c.id)
        )
        row = result.first()
    if row is None:
        raise RuntimeError("INSERT into sandbox_crashes did not yield a rowid")
    return row.id


async def list_recent(
    engine: AsyncEngine, *, limit: int = 20
) -> list[SandboxCrashRecord]:
    """Newest-first listing for the Diagnostics page."""
    if limit <= 0:
        return []
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_sandbox_crashes)
            # Tiebreak on `id` so two crashes that happened within the
            # same second (the manager's watcher polls every 5s, but
            # tests and a real crash loop can collide) still come back
            # in insertion order with the newer one first.
            .order_by(
                desc(t_sandbox_crashes.c.crashed_at),
                desc(t_sandbox_crashes.c.id),
            )
            .limit(limit)
        )
        rows = result.all()
    return [_row_to_record(r) for r in rows]
