import time

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.repository.models import Conversation
from hermes.schema import conversations as t_conversations
from hermes.schema import messages as t_messages


def _row_to_conversation(row) -> Conversation:
    return Conversation(
        id=row.id,
        channel=row.channel,
        external_id=row.external_id,
        title=row.title,
        started_at=row.started_at,
        updated_at=row.updated_at,
    )


async def create(
    conn: AsyncConnection,
    *,
    channel: str,
    external_id: str | None = None,
    title: str | None = None,
    ts: int | None = None,
) -> Conversation:
    now = ts if ts is not None else int(time.time())
    result = await conn.execute(
        t_conversations.insert()
        .values(
            channel=channel,
            external_id=external_id,
            title=title,
            started_at=now,
            updated_at=now,
        )
        .returning(t_conversations.c.id)
    )
    await conn.commit()
    row = result.first()
    if row is None:
        raise RuntimeError("INSERT into conversations did not yield a rowid")
    return Conversation(
        id=row.id,
        channel=channel,
        external_id=external_id,
        title=title,
        started_at=now,
        updated_at=now,
    )


async def get(conn: AsyncConnection, conversation_id: int) -> Conversation | None:
    result = await conn.execute(
        select(t_conversations).where(t_conversations.c.id == conversation_id)
    )
    row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def list_by_channel(
    conn: AsyncConnection,
    channel: str,
    *,
    limit: int = 20,
) -> list[Conversation]:
    result = await conn.execute(
        select(t_conversations)
        .where(t_conversations.c.channel == channel)
        .order_by(desc(t_conversations.c.updated_at))
        .limit(limit)
    )
    return [_row_to_conversation(r) for r in result]


async def list_all(
    conn: AsyncConnection,
    *,
    channel: str | None = None,
    since_unix: int | None = None,
    limit: int = 20,
) -> list[Conversation]:
    """List conversations across all channels, optionally filtered."""
    stmt = select(t_conversations)
    if channel is not None:
        stmt = stmt.where(t_conversations.c.channel == channel)
    if since_unix is not None:
        stmt = stmt.where(t_conversations.c.updated_at >= since_unix)
    stmt = stmt.order_by(desc(t_conversations.c.updated_at)).limit(limit)

    result = await conn.execute(stmt)
    return [_row_to_conversation(r) for r in result]


async def message_count(conn: AsyncConnection, conversation_id: int) -> int:
    result = await conn.execute(
        select(func.count())
        .select_from(t_messages)
        .where(t_messages.c.conversation_id == conversation_id)
    )
    row = result.first()
    return int(row[0]) if row else 0


async def touch(
    conn: AsyncConnection,
    conversation_id: int,
    *,
    ts: int | None = None,
) -> None:
    now = ts if ts is not None else int(time.time())
    await conn.execute(
        t_conversations.update()
        .where(t_conversations.c.id == conversation_id)
        .values(updated_at=now)
    )
    await conn.commit()
