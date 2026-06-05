"""Tests for _migrate_skills_add_enabled migration (Plan 37 Task 1)."""
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import _migrate_skills_add_enabled


async def _column_names(engine: AsyncEngine, table: str) -> list[str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text(f"PRAGMA table_info({table})"))
        ).all()
    return [r.name for r in rows]


@pytest.mark.asyncio
async def test_migrate_skills_add_enabled_no_op_when_present(conn):
    """Idempotent: if enabled column already exists, no error."""
    # Fresh DB already has enabled column (Plan 37 schema).
    cols = await _column_names(conn, "skills")
    assert "enabled" in cols
    # Must not raise
    await _migrate_skills_add_enabled(conn)
    cols_after = await _column_names(conn, "skills")
    assert "enabled" in cols_after


@pytest.mark.asyncio
async def test_migrate_skills_add_enabled_adds_column(conn):
    """If enabled column is missing (legacy DB), it gets added with DEFAULT 1."""
    now = int(time.time())
    # First drop the column to simulate a pre-Plan-37 DB
    async with conn.begin() as db:
        await db.execute(text("ALTER TABLE skills DROP COLUMN enabled"))

    cols = await _column_names(conn, "skills")
    assert "enabled" not in cols

    # Insert a row (without enabled column)
    async with conn.begin() as db:
        await db.execute(
            text(
                "INSERT INTO skills(slug, name, description, when_to_use, "
                "body_markdown, created_at, updated_at) "
                "VALUES ('test-skill', 'Test', 'Desc', '', 'Body', :now, :now)"
            ),
            {"now": now},
        )

    await _migrate_skills_add_enabled(conn)

    cols = await _column_names(conn, "skills")
    assert "enabled" in cols

    # Existing row must default to enabled=1
    async with conn.connect() as db:
        row = (
            await db.execute(
                text("SELECT enabled FROM skills WHERE slug='test-skill'")
            )
        ).first()
    assert row is not None
    assert row.enabled == 1

    # Idempotent: second run is a no-op
    await _migrate_skills_add_enabled(conn)
    cols_again = await _column_names(conn, "skills")
    assert "enabled" in cols_again
