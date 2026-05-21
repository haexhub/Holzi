import time

import aiosqlite

from hermes.repository.models import Conversation


def _row_to_conversation(row: aiosqlite.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        channel=row["channel"],
        external_id=row["external_id"],
        title=row["title"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
    )


async def create(
    conn: aiosqlite.Connection,
    *,
    channel: str,
    external_id: str | None = None,
    title: str | None = None,
    ts: int | None = None,
) -> Conversation:
    now = ts if ts is not None else int(time.time())
    cursor = await conn.execute(
        "INSERT INTO conversations (channel, external_id, title, started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (channel, external_id, title, now, now),
    )
    await conn.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT into conversations did not yield a rowid")
    return Conversation(
        id=cursor.lastrowid,
        channel=channel,
        external_id=external_id,
        title=title,
        started_at=now,
        updated_at=now,
    )


async def get(conn: aiosqlite.Connection, conversation_id: int) -> Conversation | None:
    async with conn.execute(
        "SELECT id, channel, external_id, title, started_at, updated_at "
        "FROM conversations WHERE id = ?",
        (conversation_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_conversation(row) if row is not None else None


async def list_by_channel(
    conn: aiosqlite.Connection,
    channel: str,
    *,
    limit: int = 20,
) -> list[Conversation]:
    async with conn.execute(
        "SELECT id, channel, external_id, title, started_at, updated_at "
        "FROM conversations WHERE channel = ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (channel, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_conversation(r) for r in rows]


async def touch(
    conn: aiosqlite.Connection,
    conversation_id: int,
    *,
    ts: int | None = None,
) -> None:
    now = ts if ts is not None else int(time.time())
    await conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (now, conversation_id),
    )
    await conn.commit()
