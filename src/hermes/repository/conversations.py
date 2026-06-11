import re
import shutil
import time
from pathlib import Path

from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.config import settings
from hermes.repository.models import Conversation
from hermes.schema import attachments as t_attachments
from hermes.schema import conversations as t_conversations
from hermes.schema import messages as t_messages

# Search input is tokenised into bare words before it reaches FTS5. The
# regex only emits `\w+` runs, so operator characters from the user
# ("*", "AND", quotes, parens) are dropped at the tokenisation step and
# can't break the MATCH parser or sneak into LIKE patterns.
_FTS_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)

_SECONDS_PER_DAY = 86_400


def _compute_expires_at(updated_at: int, *, ttl_days: int | None = None) -> int:
    """TTL expiration timestamp for a non-bookmarked conversation."""
    days = ttl_days if ttl_days is not None else settings.conversation_ttl_days
    return updated_at + days * _SECONDS_PER_DAY


def _scratch_dir(scratch_root: Path | None, conversation_id: int) -> Path | None:
    if scratch_root is None:
        return None
    return scratch_root / str(conversation_id)


def _row_to_conversation(row) -> Conversation:
    return Conversation(
        id=row.id,
        channel=row.channel,
        external_id=row.external_id,
        title=row.title,
        started_at=row.started_at,
        updated_at=row.updated_at,
        user_id=row.user_id,
        bookmarked=bool(row.bookmarked),
        expires_at=row.expires_at,
    )


async def create(
    engine: AsyncEngine,
    *,
    user_id: int,
    channel: str,
    external_id: str | None = None,
    title: str | None = None,
    ts: int | None = None,
    bookmarked: bool = False,
) -> Conversation:
    now = ts if ts is not None else int(time.time())
    expires_at = None if bookmarked else _compute_expires_at(now)
    async with engine.begin() as conn:
        result = await conn.execute(
            t_conversations.insert()
            .values(
                user_id=user_id,
                channel=channel,
                external_id=external_id,
                title=title,
                started_at=now,
                updated_at=now,
                bookmarked=1 if bookmarked else 0,
                expires_at=expires_at,
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
        user_id=user_id,
        bookmarked=bookmarked,
        expires_at=expires_at,
    )


async def get(
    engine: AsyncEngine, conversation_id: int, *, user_id: int
) -> Conversation | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_conversations).where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
        )
        row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def list_by_channel(
    engine: AsyncEngine,
    channel: str,
    *,
    user_id: int,
    limit: int = 20,
) -> list[Conversation]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_conversations)
            .where(
                t_conversations.c.channel == channel,
                t_conversations.c.user_id == user_id,
            )
            .order_by(desc(t_conversations.c.updated_at))
            .limit(limit)
        )
        rows = result.all()
    return [_row_to_conversation(r) for r in rows]


async def find_latest_by_external_id(
    engine: AsyncEngine,
    *,
    user_id: int,
    channel: str,
    external_id: str,
) -> Conversation | None:
    """Latest conversation for `(user_id, channel, external_id)`, or None.

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
                t_conversations.c.user_id == user_id,
            )
            .order_by(desc(t_conversations.c.updated_at))
            .limit(1)
        )
        row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def list_all(
    engine: AsyncEngine,
    *,
    user_id: int,
    channel: str | None = None,
    since_unix: int | None = None,
    limit: int = 20,
) -> list[Conversation]:
    """List a user's conversations across channels, optionally filtered."""
    stmt = select(t_conversations).where(t_conversations.c.user_id == user_id)
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
    user_id: int,
    query: str,
    channel: str | None = None,
    limit: int = 20,
) -> list[Conversation]:
    """Find conversations whose title or any message content matches ``query``.

    Tokenises the input into ``\\w+`` runs and treats each token as a
    prefix: title hits use case-insensitive ``LIKE %tok%`` (substring) and
    message hits use FTS5 ``tok*`` (prefix) against ``messages_fts``. The
    two hit-sets are unioned at the conversation level so a thread
    appearing in both shows up exactly once. Tokens are OR-joined on both
    sides, so multi-word queries widen the result set instead of
    narrowing it — same recall users get from chat search elsewhere.

    Results are sorted newest-first and capped at ``limit``. A blank/
    empty query falls back to :func:`list_all` so the search box can be
    "cleared" by typing whitespace; a query that is non-empty but
    contains no word characters (e.g. ``"***"``) returns an empty list
    instead, treating it as "I searched for something and there were no
    matches".
    """
    stripped = query.strip()
    if not stripped:
        return await list_all(engine, user_id=user_id, channel=channel, limit=limit)

    tokens = _FTS_TOKEN_RE.findall(stripped)
    if not tokens:
        return []

    # One LIKE per token, ORed. Mirrors the FTS5 OR semantics on the
    # message side so the title and message search behave the same way
    # for multi-token input — no surprising AND/OR asymmetry between
    # "matches in the title" and "matches in a message".
    params: dict[str, object] = {"limit": limit, "user_id": user_id}
    title_clauses: list[str] = []
    for i, tok in enumerate(tokens):
        key = f"title_pat_{i}"
        title_clauses.append(f"LOWER(c.title) LIKE :{key}")
        params[key] = f"%{tok.lower()}%"

    # `tok*` is FTS5 prefix matching, so typing "dent" finds a message
    # mentioning "dentist". Tokens are `\w+`, so no operator characters
    # can leak through to confuse the MATCH parser.
    fts_match = " OR ".join(f"{t}*" for t in tokens)
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
        "SELECT c.id, c.channel, c.external_id, c.title, c.started_at, "
        "c.updated_at, c.bookmarked, c.expires_at, c.user_id "
        "FROM conversations c "
        f"WHERE c.user_id = :user_id AND {channel_clause}({' OR '.join(conditions)}) "
        "ORDER BY c.updated_at DESC LIMIT :limit"
    )

    async with engine.connect() as conn:
        result = await conn.execute(sql, params)
        rows = result.all()
    return [_row_to_conversation(r) for r in rows]


async def message_count(
    engine: AsyncEngine, conversation_id: int, *, user_id: int
) -> int:
    """Count messages in a conversation the caller owns. A conversation that
    belongs to another user counts as 0 (its rows are invisible)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.count())
            .select_from(
                t_messages.join(
                    t_conversations,
                    t_messages.c.conversation_id == t_conversations.c.id,
                )
            )
            .where(
                t_messages.c.conversation_id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
        )
        row = result.first()
    return int(row[0]) if row else 0


async def update_title(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    user_id: int,
    title: str | None,
    ts: int | None = None,
) -> Conversation | None:
    """Rename a conversation. Touches `updated_at` and refreshes
    `expires_at` for non-bookmarked threads — a rename counts as user
    activity worth keeping the row around for. Scoped to the owner: another
    user's row returns None (no-op).
    """
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        existing = await conn.execute(
            select(t_conversations.c.bookmarked).where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
        )
        ex_row = existing.first()
        if ex_row is None:
            return None
        expires_at = None if ex_row.bookmarked else _compute_expires_at(now)
        result = await conn.execute(
            t_conversations.update()
            .where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
            .values(title=title, updated_at=now, expires_at=expires_at)
            .returning(t_conversations)
        )
        row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def delete(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    user_id: int,
    scratch_root: Path | None = None,
) -> bool:
    """Remove the conversation, its messages, and (if a scratch root is
    given) the per-conversation scratch directory. Returns False when
    the row doesn't exist or belongs to another user; the scratch dir is
    removed even when it was never materialised — `shutil.rmtree(...,
    ignore_errors=True)` is a no-op on missing paths.
    """
    async with engine.begin() as conn:
        existing = await conn.execute(
            select(t_conversations.c.id).where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
        )
        if existing.first() is None:
            return False
        # Be explicit instead of relying on SQLite FK cascade settings.
        # Attachments first (they reference messages), then messages, then
        # the conversation row. The on-disk files go with the scratch dir
        # rmtree below.
        await conn.execute(
            t_attachments.delete().where(
                t_attachments.c.conversation_id == conversation_id
            )
        )
        await conn.execute(
            t_messages.delete().where(t_messages.c.conversation_id == conversation_id)
        )
        await conn.execute(
            t_conversations.delete().where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
        )
    scratch = _scratch_dir(scratch_root, conversation_id)
    if scratch is not None:
        shutil.rmtree(scratch, ignore_errors=True)
    return True


async def touch(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    user_id: int,
    ts: int | None = None,
) -> None:
    """Mark the conversation as active. Refreshes the TTL for non-
    bookmarked threads so a chatty conversation never accidentally ages
    out mid-session. Scoped to the owner: another user's row is a no-op.
    """
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        existing = await conn.execute(
            select(t_conversations.c.bookmarked).where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
        )
        ex_row = existing.first()
        if ex_row is None:
            return
        expires_at = None if ex_row.bookmarked else _compute_expires_at(now)
        await conn.execute(
            t_conversations.update()
            .where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
            .values(updated_at=now, expires_at=expires_at)
        )


async def set_bookmarked(
    engine: AsyncEngine,
    conversation_id: int,
    *,
    user_id: int,
    bookmarked: bool,
    ts: int | None = None,
) -> Conversation | None:
    """Pin or unpin a conversation. Pinning sets `expires_at = NULL`;
    unpinning recomputes `expires_at` from the current `updated_at` so
    a long-ignored unpinned thread doesn't immediately disappear in the
    next sweep. Scoped to the owner: another user's row returns None.
    """
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        existing = await conn.execute(
            select(t_conversations.c.updated_at).where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
        )
        ex_row = existing.first()
        if ex_row is None:
            return None
        if bookmarked:
            expires_at: int | None = None
        else:
            # Reset the clock from "now" so unbookmarking a stale thread
            # gives the user the full TTL window to decide on it.
            expires_at = _compute_expires_at(now)
            # Refresh updated_at so sorting matches user intent.
        new_updated = now if not bookmarked else ex_row.updated_at
        result = await conn.execute(
            t_conversations.update()
            .where(
                t_conversations.c.id == conversation_id,
                t_conversations.c.user_id == user_id,
            )
            .values(
                bookmarked=1 if bookmarked else 0,
                expires_at=expires_at,
                updated_at=new_updated,
            )
            .returning(t_conversations)
        )
        row = result.first()
    return _row_to_conversation(row) if row is not None else None


async def list_expired(
    engine: AsyncEngine,
    *,
    now: int,
    limit: int = 500,
) -> list[Conversation]:
    """Conversations whose TTL is past `now`. Bookmarked rows have
    `expires_at = NULL` and are excluded automatically.

    Intentionally GLOBAL (not user-scoped): the TTL sweeper runs as a
    background job over every user's expired rows in Wave C1.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_conversations)
            .where(t_conversations.c.expires_at.is_not(None))
            .where(t_conversations.c.expires_at <= now)
            .order_by(t_conversations.c.expires_at)
            .limit(limit)
        )
        rows = result.all()
    return [_row_to_conversation(r) for r in rows]


async def sweep_expired(
    engine: AsyncEngine,
    *,
    now: int,
    scratch_root: Path | None = None,
    limit: int = 500,
) -> list[int]:
    """Delete every conversation whose `expires_at` is past `now`.
    Returns the IDs deleted. Bookmarked rows are skipped (NULL filter).
    Each row's scratch directory is removed too when `scratch_root` is
    given.

    Intentionally GLOBAL (not user-scoped): the sweeper deletes every
    user's expired rows. Each delete passes the row's own `user_id` so
    the scoped `delete` still targets the correct owner.
    """
    expired = await list_expired(engine, now=now, limit=limit)
    deleted: list[int] = []
    for c in expired:
        if await delete(engine, c.id, user_id=c.user_id, scratch_root=scratch_root):
            deleted.append(c.id)
    return deleted
