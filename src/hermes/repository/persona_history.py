"""Persistence layer for `persona_history` (Plan 36).

Append-only audit log of persona writes. The personas repo calls
`write_snapshot` from inside its own transaction so the history row is
atomic with the persona INSERT/UPDATE that produced it; routes and the
future history-view UI use `list_for_persona` and `get` for read-side
access.

`snapshot_json` deliberately stores only `{name, soul, identity, agents}`:
`is_default` is a sorting flag, not part of a persona's identity, and
including it would create misleading audit rows whenever the default
flag flips between rows.
"""
import json
import time

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hermes.db import tx_for_user
from hermes.repository.models import Persona, PersonaHistory
from hermes.schema import persona_history as t_history


def _row_to_history(row) -> PersonaHistory:
    return PersonaHistory(
        id=row.id,
        persona_id=row.persona_id,
        author=row.author,
        snapshot_json=row.snapshot_json,
        created_at=row.created_at,
    )


def _persona_snapshot_json(persona: Persona) -> str:
    return json.dumps(
        {
            "name": persona.name,
            "soul": persona.soul,
            "identity": persona.identity,
            "agents": persona.agents,
        }
    )


async def _do_write(
    conn: AsyncConnection,
    *,
    persona_id: int,
    user_id: int,
    author: str,
    snapshot_json: str,
    now: int,
) -> PersonaHistory:
    result = await conn.execute(
        t_history.insert()
        .values(
            persona_id=persona_id,
            author=author,
            snapshot_json=snapshot_json,
            created_at=now,
            user_id=user_id,
        )
        .returning(
            t_history.c.id,
            t_history.c.persona_id,
            t_history.c.author,
            t_history.c.snapshot_json,
            t_history.c.created_at,
        )
    )
    row = result.first()
    if row is None:
        raise RuntimeError(
            "persona_history insert ... RETURNING returned no row"
        )
    return _row_to_history(row)


async def write_snapshot(
    engine: AsyncEngine,
    persona: Persona,
    *,
    author: str = "user",
    ts: int | None = None,
    conn: AsyncConnection | None = None,
) -> PersonaHistory:
    """Append one history row for `persona`.

    `snapshot_json` is `{"name", "soul", "identity", "agents"}` — note
    that `is_default` is intentionally excluded (sorting flag, not
    identity).

    If `conn` is supplied, the INSERT runs on that existing connection so
    the snapshot lands in the same transaction as the caller's write
    (used by the personas repo's `create`/`update`). When `conn` is
    None, a fresh `tx_for_user` block is opened — the persona's own
    `user_id` is propagated so RLS scopes the audit row to its owner.
    """
    now = ts if ts is not None else int(time.time())
    snapshot_json = _persona_snapshot_json(persona)

    if conn is not None:
        return await _do_write(
            conn,
            persona_id=persona.id,
            user_id=persona.user_id,
            author=author,
            snapshot_json=snapshot_json,
            now=now,
        )

    async with tx_for_user(engine, user_id=persona.user_id) as new_conn:
        return await _do_write(
            new_conn,
            persona_id=persona.id,
            user_id=persona.user_id,
            author=author,
            snapshot_json=snapshot_json,
            now=now,
        )


async def list_for_persona(
    engine: AsyncEngine, persona_id: int, *, user_id: int
) -> list[PersonaHistory]:
    """All history rows for `persona_id`, newest first.

    Ties on `created_at` are broken by `id DESC` so a burst of writes
    in the same second still surfaces in the order they were inserted.
    """
    stmt = (
        select(t_history)
        .where(t_history.c.persona_id == persona_id)
        .order_by(desc(t_history.c.created_at), desc(t_history.c.id))
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_history(r) for r in rows]


async def get(
    engine: AsyncEngine, history_id: int, *, user_id: int
) -> PersonaHistory | None:
    """Fetch one row by id, or None when no such row exists."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_history).where(t_history.c.id == history_id)
        )
        row = result.first()
    return _row_to_history(row) if row is not None else None
