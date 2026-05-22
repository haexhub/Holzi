import time

from sqlalchemy import asc, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Message
from hermes.schema import messages as t_messages


def _row_to_message(row) -> Message:
    return Message(
        id=row.id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        ts=row.ts,
        meta_json=row.meta_json,
    )


async def append(
    engine: AsyncEngine,
    *,
    conversation_id: int,
    role: str,
    content: str,
    ts: int | None = None,
    meta_json: str | None = None,
) -> Message:
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        result = await conn.execute(
            t_messages.insert()
            .values(
                conversation_id=conversation_id,
                role=role,
                content=content,
                ts=now,
                meta_json=meta_json,
            )
            .returning(t_messages.c.id)
        )
        row = result.first()
    if row is None:
        raise RuntimeError("INSERT into messages did not yield a rowid")
    return Message(
        id=row.id,
        conversation_id=conversation_id,
        role=role,
        content=content,
        ts=now,
        meta_json=meta_json,
    )


async def list_by_conversation(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    limit: int = 50,
) -> list[Message]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_messages)
            .where(t_messages.c.conversation_id == conversation_id)
            .order_by(asc(t_messages.c.ts), asc(t_messages.c.id))
            .limit(limit)
        )
        rows = result.all()
    return [_row_to_message(r) for r in rows]


async def fts_search(
    engine: AsyncEngine,
    *,
    query: str,
    conversation_id: int | None = None,
    limit: int = 10,
) -> list[Message]:
    # FTS5 isn't modelled by SQLAlchemy Core — fall back to raw SQL via
    # `text()`. Parameters are bound, so no SQL injection.
    if conversation_id is None:
        sql = text(
            "SELECT m.id, m.conversation_id, m.role, m.content, m.ts, m.meta_json "
            "FROM messages m JOIN messages_fts f ON f.rowid = m.id "
            "WHERE messages_fts MATCH :q "
            "ORDER BY rank LIMIT :limit"
        )
        params = {"q": query, "limit": limit}
    else:
        sql = text(
            "SELECT m.id, m.conversation_id, m.role, m.content, m.ts, m.meta_json "
            "FROM messages m JOIN messages_fts f ON f.rowid = m.id "
            "WHERE messages_fts MATCH :q AND m.conversation_id = :cid "
            "ORDER BY rank LIMIT :limit"
        )
        params = {"q": query, "cid": conversation_id, "limit": limit}

    async with engine.connect() as conn:
        result = await conn.execute(sql, params)
        rows = result.all()
    return [_row_to_message(r) for r in rows]
