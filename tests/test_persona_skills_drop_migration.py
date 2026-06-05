"""Tests for _drop_persona_skills_table migration (Plan 37 Task 1)."""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import _drop_persona_skills_table


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name=:name"
                ),
                {"name": table},
            )
        ).all()
    return len(rows) > 0


@pytest.mark.asyncio
async def test_drop_persona_skills_no_op_when_missing(conn):
    """Idempotent: if table doesn't exist, no error and no crash."""
    # Fresh DB from fixture never has persona_skills (Plan 37 schema).
    assert not await _table_exists(conn, "persona_skills")
    # Must not raise
    await _drop_persona_skills_table(conn)
    assert not await _table_exists(conn, "persona_skills")


@pytest.mark.asyncio
async def test_drop_persona_skills_removes_table(conn):
    """Table is created manually (simulating Plan-33 legacy DB), then dropped."""
    # Manually create the table to simulate a pre-Plan-37 DB
    async with conn.begin() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS persona_skills "
                "(persona_id INTEGER, skill_id INTEGER, "
                "ordering INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, "
                "PRIMARY KEY (persona_id, skill_id))"
            )
        )
        await db.execute(
            text(
                "INSERT INTO persona_skills(persona_id, skill_id, ordering, enabled) "
                "VALUES (1, 1, 0, 1)"
            )
        )

    assert await _table_exists(conn, "persona_skills")
    await _drop_persona_skills_table(conn)
    assert not await _table_exists(conn, "persona_skills")

    # Idempotent: second run is a no-op
    await _drop_persona_skills_table(conn)
    assert not await _table_exists(conn, "persona_skills")


@pytest.mark.asyncio
async def test_drop_persona_skills_leaves_skills_intact(conn):
    """Dropping persona_skills must not touch the skills table."""
    async with conn.begin() as db:
        await db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS persona_skills "
                "(persona_id INTEGER, skill_id INTEGER, PRIMARY KEY (persona_id, skill_id))"
            )
        )

    # Verify skills table still exists and is queryable
    assert await _table_exists(conn, "skills")
    await _drop_persona_skills_table(conn)
    assert await _table_exists(conn, "skills")
