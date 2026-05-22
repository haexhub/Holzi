import time

from sqlalchemy import desc, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Note
from hermes.schema import notes as t_notes


def _row_to_note(row) -> Note:
    return Note(
        id=row.id,
        key=row.key,
        content=row.content,
        tags=row.tags,
        updated_at=row.updated_at,
    )


async def upsert(
    engine: AsyncEngine,
    *,
    key: str,
    content: str,
    tags: str | None = None,
    ts: int | None = None,
) -> Note:
    now = ts if ts is not None else int(time.time())
    insert_stmt = sqlite_insert(t_notes).values(
        key=key, content=content, tags=tags, updated_at=now
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[t_notes.c.key],
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
    )
    async with engine.begin() as conn:
        result = await conn.execute(upsert_stmt)
        row = result.first()
    if row is None:
        raise RuntimeError("upsert into notes ... RETURNING returned no row")
    return _row_to_note(row)


async def get(engine: AsyncEngine, key: str) -> Note | None:
    async with engine.connect() as conn:
        result = await conn.execute(select(t_notes).where(t_notes.c.key == key))
        row = result.first()
    return _row_to_note(row) if row is not None else None


async def list_all(
    engine: AsyncEngine,
    *,
    limit: int = 100,
) -> list[Note]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_notes).order_by(desc(t_notes.c.updated_at)).limit(limit)
        )
        rows = result.all()
    return [_row_to_note(r) for r in rows]


async def delete(engine: AsyncEngine, key: str) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(t_notes.delete().where(t_notes.c.key == key))
    return (result.rowcount or 0) > 0


async def find(
    engine: AsyncEngine,
    *,
    query: str,
    limit: int = 10,
) -> list[Note]:
    # FTS5 join — raw SQL via `text()`, parameters bound.
    sql = text(
        "SELECT n.id, n.key, n.content, n.tags, n.updated_at "
        "FROM notes n JOIN notes_fts f ON f.rowid = n.id "
        "WHERE notes_fts MATCH :q "
        "ORDER BY rank LIMIT :limit"
    )
    async with engine.connect() as conn:
        result = await conn.execute(sql, {"q": query, "limit": limit})
        rows = result.all()
    return [_row_to_note(r) for r in rows]
