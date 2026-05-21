import time

import aiosqlite

from hermes.repository.models import Note


def _row_to_note(row: aiosqlite.Row) -> Note:
    return Note(
        id=row["id"],
        key=row["key"],
        content=row["content"],
        tags=row["tags"],
        updated_at=row["updated_at"],
    )


async def upsert(
    conn: aiosqlite.Connection,
    *,
    key: str,
    content: str,
    tags: str | None = None,
    ts: int | None = None,
) -> Note:
    now = ts if ts is not None else int(time.time())
    async with conn.execute(
        "INSERT INTO notes (key, content, tags, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "  content = excluded.content, "
        "  tags = excluded.tags, "
        "  updated_at = excluded.updated_at "
        "RETURNING id, key, content, tags, updated_at",
        (key, content, tags, now),
    ) as cursor:
        row = await cursor.fetchone()
    await conn.commit()
    if row is None:
        raise RuntimeError("upsert into notes ... RETURNING returned no row")
    return _row_to_note(row)


async def get(conn: aiosqlite.Connection, key: str) -> Note | None:
    async with conn.execute(
        "SELECT id, key, content, tags, updated_at FROM notes WHERE key = ?",
        (key,),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_note(row) if row is not None else None


async def find(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int = 10,
) -> list[Note]:
    async with conn.execute(
        "SELECT n.id, n.key, n.content, n.tags, n.updated_at "
        "FROM notes n JOIN notes_fts f ON f.rowid = n.id "
        "WHERE notes_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (query, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_note(r) for r in rows]
