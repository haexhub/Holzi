import time

from sqlalchemy import desc, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.db import tx_for_user
from hermes.repository.models import Note
from hermes.schema import notes as t_notes


def _row_to_note(row) -> Note:
    return Note(
        id=row.id,
        key=row.key,
        content=row.content,
        tags=row.tags,
        updated_at=row.updated_at,
        user_id=row.user_id,
    )


async def upsert(
    engine: AsyncEngine,
    *,
    user_id: int,
    key: str,
    content: str,
    tags: str | None = None,
    ts: int | None = None,
) -> Note:
    now = ts if ts is not None else int(time.time())
    insert_stmt = pg_insert(t_notes).values(
        user_id=user_id, key=key, content=content, tags=tags, updated_at=now
    )
    # Conflict target is the per-user (user_id, key) constraint — on a fresh
    # DB the same key can exist for different users; the upsert only updates
    # the caller's own row.
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[t_notes.c.user_id, t_notes.c.key],
        set_={
            "content": insert_stmt.excluded.content,
            "tags": insert_stmt.excluded.tags,
            "updated_at": insert_stmt.excluded.updated_at,
        },
    ).returning(
        t_notes.c.id,
        t_notes.c.key,
        t_notes.c.content,
        t_notes.c.tags,
        t_notes.c.updated_at,
        t_notes.c.user_id,
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(upsert_stmt)
        row = result.first()
    if row is None:
        raise RuntimeError("upsert into notes ... RETURNING returned no row")
    return _row_to_note(row)


async def get(engine: AsyncEngine, key: str, *, user_id: int) -> Note | None:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_notes).where(
                t_notes.c.key == key,
                t_notes.c.user_id == user_id,
            )
        )
        row = result.first()
    return _row_to_note(row) if row is not None else None


async def list_all(
    engine: AsyncEngine,
    *,
    user_id: int,
    limit: int = 100,
) -> list[Note]:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_notes)
            .where(t_notes.c.user_id == user_id)
            .order_by(desc(t_notes.c.updated_at))
            .limit(limit)
        )
        rows = result.all()
    return [_row_to_note(r) for r in rows]


async def delete(engine: AsyncEngine, key: str, *, user_id: int) -> bool:
    """Remove a note the caller owns. A note belonging to another user is a
    no-op (returns False) — the `user_id` filter is part of the DELETE WHERE."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            t_notes.delete().where(
                t_notes.c.key == key,
                t_notes.c.user_id == user_id,
            )
        )
    return (result.rowcount or 0) > 0


async def find(
    engine: AsyncEngine,
    *,
    user_id: int,
    query: str,
    limit: int = 10,
) -> list[Note]:
    """Full-text search over notes (key + content + tags), using the
    generated `search_tsv` GIN index. `query` is a Postgres `tsquery`
    expression — callers tokenise free-form user input first. The
    `n.user_id` filter is defense-in-depth on top of RLS: RLS already
    scopes the row set, but the explicit predicate lets the planner skip
    rows owned by other users without consulting policy.
    """
    sql = text(
        "SELECT id, key, content, tags, updated_at, user_id "
        "FROM notes "
        "WHERE search_tsv @@ to_tsquery('simple', :q) AND user_id = :user_id "
        "ORDER BY ts_rank(search_tsv, to_tsquery('simple', :q)) DESC "
        "LIMIT :limit"
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            sql, {"q": query, "limit": limit, "user_id": user_id}
        )
        rows = result.all()
    return [_row_to_note(r) for r in rows]
