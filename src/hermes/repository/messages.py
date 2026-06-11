import time

from sqlalchemy import asc, desc, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.db import tx_for_user
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
    user_id: int,
    conversation_id: int,
    role: str,
    content: str,
    ts: int | None = None,
    meta_json: str | None = None,
) -> Message:
    """Append a row to `messages`.

    `user_id` is denormalised from the parent conversation (Plan §1) so
    RLS can scope without a join. The caller looks the value up once in
    the route layer and threads it through.
    """
    now = ts if ts is not None else int(time.time())
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            t_messages.insert()
            .values(
                conversation_id=conversation_id,
                role=role,
                content=content,
                ts=now,
                meta_json=meta_json,
                user_id=user_id,
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
    user_id: int,
    limit: int = 50,
) -> list[Message]:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_messages)
            .where(t_messages.c.conversation_id == conversation_id)
            .order_by(asc(t_messages.c.ts), asc(t_messages.c.id))
            .limit(limit)
        )
        rows = result.all()
    return [_row_to_message(r) for r in rows]


async def last_user_message(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    user_id: int,
) -> Message | None:
    """Return the most recently appended user message in the conversation,
    or None if it has no user turn yet."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_messages)
            .where(
                t_messages.c.conversation_id == conversation_id,
                t_messages.c.role == "user",
            )
            .order_by(desc(t_messages.c.id))
            .limit(1)
        )
        row = result.first()
    return _row_to_message(row) if row is not None else None


async def get(engine: AsyncEngine, message_id: int, *, user_id: int) -> Message | None:
    """Return the message with the given id, or None if it does not exist."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_messages).where(t_messages.c.id == message_id)
        )
        row = result.first()
    return _row_to_message(row) if row is not None else None


async def update_content(
    engine: AsyncEngine,
    message_id: int,
    *,
    user_id: int,
    content: str,
) -> Message | None:
    """Replace a message's content in place, keeping its role and ts so the
    edited turn stays in chronological position. Returns the updated message,
    or None if no such id exists. The FTS index follows via the AFTER UPDATE
    trigger on `messages`."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            t_messages.update()
            .where(t_messages.c.id == message_id)
            .values(content=content)
            .returning(*t_messages.c)
        )
        row = result.first()
    return _row_to_message(row) if row is not None else None


async def delete_after(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    user_id: int,
    after_id: int,
) -> int:
    """Delete every message in the conversation whose id is greater than
    `after_id`, returning how many rows were removed.

    Messages are append-only with autoincrement ids, so `id > after_id`
    is exactly the tail that follows `after_id` chronologically. The FTS
    index is kept in sync by the AFTER DELETE trigger on `messages`.
    """
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            t_messages.delete().where(
                t_messages.c.conversation_id == conversation_id,
                t_messages.c.id > after_id,
            )
        )
    return result.rowcount


async def fts_search(
    engine: AsyncEngine,
    *,
    user_id: int,
    query: str,
    conversation_id: int | None = None,
    limit: int = 10,
) -> list[Message]:
    """Full-text search across messages using the `content_tsv` GIN index.

    `query` is a Postgres `tsquery` expression — the caller is expected to
    tokenise free-form user input into safe tokens and join them with `|`
    / `&` before reaching here (operator chars from raw user input would
    otherwise raise `SyntaxError` from `to_tsquery`). The `user_id =
    :uid` filter is defense-in-depth — RLS already scopes the row set,
    but the explicit predicate lets the planner skip rows owned by other
    users without consulting policy.
    """
    sql_base = (
        "SELECT id, conversation_id, role, content, ts, meta_json "
        "FROM messages "
        "WHERE content_tsv @@ to_tsquery('simple', :q) "
        "AND user_id = :uid"
    )
    params: dict[str, object] = {"q": query, "uid": user_id, "limit": limit}
    if conversation_id is not None:
        sql_base += " AND conversation_id = :cid"
        params["cid"] = conversation_id
    sql_base += " ORDER BY ts DESC LIMIT :limit"

    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(text(sql_base), params)
        rows = result.all()
    return [_row_to_message(r) for r in rows]
