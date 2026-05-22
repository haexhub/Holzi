from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.db import init_db


async def _table_names(conn: AsyncConnection) -> set[str]:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    )
    return {row[0] for row in result}


async def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    conn = await init_db(str(tmp_path / "hermes.db"))
    try:
        tables = await _table_names(conn)
        assert {"conversations", "messages", "notes"} <= tables
        # FTS5 virtual tables show up as tables in sqlite_master too.
        assert {"messages_fts", "notes_fts"} <= tables
    finally:
        await conn.close()


async def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "hermes.db")
    conn1 = await init_db(db_path)
    await conn1.close()
    conn2 = await init_db(db_path)
    try:
        tables = await _table_names(conn2)
        assert {"conversations", "messages", "notes"} <= tables
    finally:
        await conn2.close()


async def test_init_db_enables_foreign_keys(tmp_path: Path) -> None:
    conn = await init_db(str(tmp_path / "hermes.db"))
    try:
        result = await conn.execute(text("PRAGMA foreign_keys"))
        row = result.first()
        assert row is not None and row[0] == 1
    finally:
        await conn.close()
