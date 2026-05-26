import re
import time

from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Conversation
from hermes.schema import conversations as t_conversations
from hermes.schema import messages as t_messages

# Tokens fed to FTS5 MATCH must be bare words — wrapping each in double
# quotes makes the parser treat user input as literal phrases instead of
# operators ("*", "AND", parentheses, quotes...) so a search box never
# triggers a SQL error from unbalanced syntax.
_FTS_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


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
    engine: AsyncEngine,
    *,
    channel: str,
    external_id: str | None = None,
    title: str | None = None,
    ts: int | None = None,
) -> Conversation:
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
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


async def get(engine: AsyncEngine, conversation_id: int) -> Conversation | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_conversations).where(t_conversations.c.id == conversation_id)
        )
        row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def list_by_channel(
    engine: AsyncEngine,
    channel: str,
    *,
    limit: int = 20,
) -> list[Conversation]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_conversations)
            .where(t_conversations.c.channel == channel)
            .order_by(desc(t_conversations.c.updated_at))
            .limit(limit)
        )
        rows = result.all()
    return [_row_to_conversation(r) for r in rows]


async def find_latest_by_external_id(
    engine: AsyncEngine,
    *,
    channel: str,
    external_id: str,
) -> Conversation | None:
    """Latest conversation for `(channel, external_id)`, or None.

    Used by the Telegram worker to thread per-chat: every incoming
    chat_id maps to its own conversation row via `external_id="tg:<id>"`,
    distinct from other chats and from web/vscode sessions.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_conversations)
            .where(
                t_conversations.c.channel == channel,
                t_conversations.c.external_id == external_id,
            )
            .order_by(desc(t_conversations.c.updated_at))
            .limit(1)
        )
        row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def list_all(
    engine: AsyncEngine,
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

    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_conversation(r) for r in rows]


async def search(
    engine: AsyncEngine,
    *,
    query: str,
    channel: str | None = None,
    limit: int = 20,
) -> list[Conversation]:
    """Find conversations whose title or any message content matches ``query``.

    Title hits use a case-insensitive ``LIKE`` substring scan; message hits use
    SQLite FTS5 against ``messages_fts``. The two hit-sets are unioned at the
    conversation level so a thread appearing in both shows up exactly once.
    Results are sorted newest-first and capped at ``limit``.

    A blank/empty query (or one made entirely of FTS-meaningless punctuation)
    falls back to :func:`list_all` so the search box can be "cleared" by
    typing whitespace without producing a 400.
    """
    stripped = query.strip()
    if not stripped:
        return await list_all(engine, channel=channel, limit=limit)

    tokens = _FTS_TOKEN_RE.findall(stripped)
    if not tokens:
        return []

    # One LIKE per token, ORed. Matches the FTS5 OR semantics we'd get if we
    # ran the same tokens against the message index, and means punctuation
    # ("*", quotes) from the user input doesn't sneak into the LIKE pattern
    # and miss otherwise-valid titles.
    params: dict[str, object] = {"limit": limit}
    title_clauses: list[str] = []
    for i, tok in enumerate(tokens):
        key = f"title_pat_{i}"
        title_clauses.append(f"LOWER(c.title) LIKE :{key}")
        params[key] = f"%{tok.lower()}%"

    fts_match = " ".join(f'"{t}"' for t in tokens)
    params["fts_q"] = fts_match

    # Build the WHERE on the conversations table so SELECT produces full
    # conversation rows (no DISTINCT needed — we filter on c.id).
    conditions = [
        "(" + " OR ".join(title_clauses) + ")",
        (
            "c.id IN ("
            "SELECT m.conversation_id FROM messages m "
            "JOIN messages_fts f ON f.rowid = m.id "
            "WHERE messages_fts MATCH :fts_q"
            ")"
        ),
    ]

    channel_clause = ""
    if channel is not None:
        channel_clause = "c.channel = :channel AND "
        params["channel"] = channel

    sql = text(
        "SELECT c.id, c.channel, c.external_id, c.title, c.started_at, c.updated_at "
        "FROM conversations c "
        f"WHERE {channel_clause}({' OR '.join(conditions)}) "
        "ORDER BY c.updated_at DESC LIMIT :limit"
    )

    async with engine.connect() as conn:
        result = await conn.execute(sql, params)
        rows = result.all()
    return [_row_to_conversation(r) for r in rows]


async def message_count(engine: AsyncEngine, conversation_id: int) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.count())
            .select_from(t_messages)
            .where(t_messages.c.conversation_id == conversation_id)
        )
        row = result.first()
    return int(row[0]) if row else 0


async def update_title(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    title: str | None,
    ts: int | None = None,
) -> Conversation | None:
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        result = await conn.execute(
            t_conversations.update()
            .where(t_conversations.c.id == conversation_id)
            .values(title=title, updated_at=now)
            .returning(t_conversations)
        )
        row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def delete(engine: AsyncEngine, conversation_id: int) -> bool:
    async with engine.begin() as conn:
        existing = await conn.execute(
            select(t_conversations.c.id).where(t_conversations.c.id == conversation_id)
        )
        if existing.first() is None:
            return False
        # Be explicit instead of relying on SQLite FK cascade settings.
        await conn.execute(
            t_messages.delete().where(t_messages.c.conversation_id == conversation_id)
        )
        await conn.execute(
            t_conversations.delete().where(t_conversations.c.id == conversation_id)
        )
    return True


async def touch(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    ts: int | None = None,
) -> None:
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            t_conversations.update()
            .where(t_conversations.c.id == conversation_id)
            .values(updated_at=now)
        )
