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
