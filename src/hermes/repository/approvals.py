"""Plan 21 — persistence layer for `allow_always` tool approvals.

Session-scope (`allow_session`) lives purely on
`app.state.session_approvals`; only the always-scope survives a restart and
therefore needs a DB row. Composite PK `(user_id, tool_name)` — the same
tool can be approved per-user.
"""
import time

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.db import tx_for_user
from hermes.repository.models import ToolApproval
from hermes.schema import tool_approvals as t_tool_approvals


def _row_to_approval(row) -> ToolApproval:
    return ToolApproval(
        tool_name=row.tool_name,
        granted_at=row.granted_at,
        last_used_at=row.last_used_at,
    )


async def grant_always(
    engine: AsyncEngine,
    tool_name: str,
    *,
    user_id: int,
    now: int | None = None,
) -> ToolApproval:
    """Upsert an always-scope grant for `(user_id, tool_name)`. Re-granting
    refreshes `granted_at` without creating a duplicate row."""
    ts = now if now is not None else int(time.time())
    insert_stmt = pg_insert(t_tool_approvals).values(
        user_id=user_id, tool_name=tool_name, granted_at=ts, last_used_at=None
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[t_tool_approvals.c.user_id, t_tool_approvals.c.tool_name],
        set_={"granted_at": insert_stmt.excluded.granted_at},
    ).returning(
        t_tool_approvals.c.tool_name,
        t_tool_approvals.c.granted_at,
        t_tool_approvals.c.last_used_at,
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(upsert_stmt)
        row = result.first()
    if row is None:
        raise RuntimeError("upsert into tool_approvals ... RETURNING returned no row")
    return _row_to_approval(row)


async def is_always_allowed(
    engine: AsyncEngine, tool_name: str, *, user_id: int
) -> bool:
    """True iff a `tool_approvals` row exists for `(user_id, tool_name)`."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_tool_approvals.c.tool_name).where(
                t_tool_approvals.c.tool_name == tool_name,
                t_tool_approvals.c.user_id == user_id,
            )
        )
        return result.first() is not None


async def revoke_always(
    engine: AsyncEngine, tool_name: str, *, user_id: int
) -> bool:
    """Drop the always-scope grant for `(user_id, tool_name)`. Returns True
    if a row was removed (caller surfaces a missing tool as 404)."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            delete(t_tool_approvals).where(
                t_tool_approvals.c.tool_name == tool_name,
                t_tool_approvals.c.user_id == user_id,
            )
        )
    return result.rowcount > 0


async def list_always(
    engine: AsyncEngine, *, user_id: int
) -> list[ToolApproval]:
    """All always-scope grants for `user_id`, ordered by `granted_at`
    ascending so the UI shows oldest standing permissions first."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_tool_approvals)
            .where(t_tool_approvals.c.user_id == user_id)
            .order_by(t_tool_approvals.c.granted_at)
        )
        return [_row_to_approval(row) for row in result.all()]
