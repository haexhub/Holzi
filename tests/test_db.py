from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.db import init_db


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        return {row[0] for row in result}


async def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    engine = await init_db(str(tmp_path / "hermes.db"))
    try:
        tables = await _table_names(engine)
        assert {"conversations", "messages", "notes"} <= tables
        # FTS5 virtual tables show up as tables in sqlite_master too.
        assert {"messages_fts", "notes_fts"} <= tables
    finally:
        await engine.dispose()


async def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "hermes.db")
    engine1 = await init_db(db_path)
    await engine1.dispose()
    engine2 = await init_db(db_path)
    try:
        tables = await _table_names(engine2)
        assert {"conversations", "messages", "notes"} <= tables
    finally:
        await engine2.dispose()


async def test_init_db_enables_foreign_keys(tmp_path: Path) -> None:
    engine = await init_db(str(tmp_path / "hermes.db"))
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA foreign_keys"))
            row = result.first()
            assert row is not None and row[0] == 1
    finally:
        await engine.dispose()


async def test_migration_backfills_expires_at_for_legacy_conversations(
    tmp_path: Path,
) -> None:
    """A DB created before plan 01b adds the retention columns later;
    legacy rows must end up with a real `expires_at` so the sweep can
    actually age them out. Without the backfill the rows stay NULL and
    the retention feature is silently inert on every existing deployment.
    """
    from hermes.config import settings

    db_path = str(tmp_path / "legacy.db")
    # Stand up a pre-migration shape by hand: no `bookmarked`/`expires_at`.
    pre_engine = await init_db(db_path)
    async with pre_engine.begin() as conn:
        await conn.execute(text("DROP TABLE conversations"))
        await conn.execute(
            text(
                "CREATE TABLE conversations ("
                "id INTEGER PRIMARY KEY, "
                "channel TEXT NOT NULL, "
                "external_id TEXT, "
                "title TEXT, "
                "started_at INTEGER NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO conversations(channel, started_at, updated_at) "
                "VALUES ('web', 1000, 2000)"
            )
        )
    await pre_engine.dispose()

    # Re-open: the migration must add the new columns AND backfill.
    engine = await init_db(db_path)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT bookmarked, expires_at, updated_at "
                    "FROM conversations"
                )
            )
            row = result.first()
        assert row is not None
        assert row.bookmarked == 0
        assert row.updated_at == 2000
        assert row.expires_at == 2000 + settings.conversation_ttl_days * 86_400
    finally:
        await engine.dispose()
