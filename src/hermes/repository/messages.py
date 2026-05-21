import time

import aiosqlite

from hermes.repository.models import Message


def _row_to_message(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        ts=row["ts"],
        meta_json=row["meta_json"],
    )


async def append(
    conn: aiosqlite.Connection,
    *,
    conversation_id: int,
    role: str,
    content: str,
    ts: int | None = None,
    meta_json: str | None = None,
) -> Message:
    now = ts if ts is not None else int(time.time())
    cursor = await conn.execute(
        "INSERT INTO messages (conversation_id, role, content, ts, meta_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversation_id, role, content, now, meta_json),
    )
    await conn.commit()
    assert cursor.lastrowid is not None
    return Message(
        id=cursor.lastrowid,
        conversation_id=conversation_id,
        role=role,
        content=content,
        ts=now,
        meta_json=meta_json,
    )


async def list_by_conversation(
    conn: aiosqlite.Connection,
    conversation_id: int,
    *,
    limit: int = 50,
) -> list[Message]:
    async with conn.execute(
        "SELECT id, conversation_id, role, content, ts, meta_json "
        "FROM messages WHERE conversation_id = ? "
        "ORDER BY ts ASC, id ASC LIMIT ?",
        (conversation_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_message(r) for r in rows]


async def fts_search(
    conn: aiosqlite.Connection,
    *,
    query: str,
    conversation_id: int | None = None,
    limit: int = 10,
) -> list[Message]:
    if conversation_id is None:
        sql = (
            "SELECT m.id, m.conversation_id, m.role, m.content, m.ts, m.meta_json "
            "FROM messages m JOIN messages_fts f ON f.rowid = m.id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY rank LIMIT ?"
        )
        params: tuple = (query, limit)
    else:
        sql = (
            "SELECT m.id, m.conversation_id, m.role, m.content, m.ts, m.meta_json "
            "FROM messages m JOIN messages_fts f ON f.rowid = m.id "
            "WHERE messages_fts MATCH ? AND m.conversation_id = ? "
            "ORDER BY rank LIMIT ?"
        )
        params = (query, conversation_id, limit)

    async with conn.execute(sql, params) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_message(r) for r in rows]
