"""Tests for `_migrate_prompt_to_fragments` (Plan 36).

The migration helper is one-shot: the first boot after the schema split
copies `personas.prompt` into `identity` and drops the old column. On
subsequent boots (and on fresh DBs) it must be a no-op.

These tests build the pre- and post-migration table shapes via raw SQL
so the helper is exercised in isolation from `init_db` / `create_all`.
"""
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from hermes.personas import _migrate_prompt_to_fragments


async def _column_names(engine, table: str) -> set[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
    return {row.name for row in rows}


@pytest.mark.asyncio
async def test_migrate_copies_prompt_to_identity_and_drops_column(
    tmp_path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pre-plan36.db'}"
    )
    try:
        # Build a pre-Plan-36 `personas` table by hand (old `prompt`
        # column, no soul/identity/agents).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE personas ("
                    "  id INTEGER PRIMARY KEY,"
                    "  name TEXT UNIQUE NOT NULL,"
                    "  prompt TEXT NOT NULL,"
                    "  is_default INTEGER NOT NULL DEFAULT 0,"
                    "  created_at INTEGER NOT NULL,"
                    "  updated_at INTEGER NOT NULL"
                    ")"
                )
            )
            now = int(time.time())
            await conn.execute(
                text(
                    "INSERT INTO personas "
                    "(name, prompt, is_default, created_at, updated_at) "
                    "VALUES (:name, :prompt, 1, :now, :now)"
                ),
                {"name": "Legacy", "prompt": "legacy-text", "now": now},
            )

        # Sanity: old shape in place.
        cols_before = await _column_names(engine, "personas")
        assert "prompt" in cols_before
        assert "identity" not in cols_before

        # The helper expects the new columns to already exist on the
        # table (schema.py would have created them via metadata.create_all
        # at this point in a real boot). Simulate that by ALTER-adding
        # them before calling the migration.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE personas ADD COLUMN soul TEXT "
                    "NOT NULL DEFAULT ''"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE personas ADD COLUMN identity TEXT "
                    "NOT NULL DEFAULT ''"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE personas ADD COLUMN agents TEXT "
                    "NOT NULL DEFAULT ''"
                )
            )

        await _migrate_prompt_to_fragments(engine)

        cols_after = await _column_names(engine, "personas")
        assert "prompt" not in cols_after
        assert {"soul", "identity", "agents"} <= cols_after

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT name, soul, identity, agents "
                        "FROM personas WHERE name = 'Legacy'"
                    )
                )
            ).one()
        assert row.soul == ""
        assert row.identity == "legacy-text"
        assert row.agents == ""
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migrate_idempotent_on_already_migrated_db(tmp_path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'post-plan36.db'}"
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE personas ("
                    "  id INTEGER PRIMARY KEY,"
                    "  name TEXT UNIQUE NOT NULL,"
                    "  soul TEXT NOT NULL DEFAULT '',"
                    "  identity TEXT NOT NULL DEFAULT '',"
                    "  agents TEXT NOT NULL DEFAULT '',"
                    "  is_default INTEGER NOT NULL DEFAULT 0,"
                    "  created_at INTEGER NOT NULL,"
                    "  updated_at INTEGER NOT NULL"
                    ")"
                )
            )
            now = int(time.time())
            await conn.execute(
                text(
                    "INSERT INTO personas "
                    "(name, soul, identity, agents, is_default, "
                    " created_at, updated_at) "
                    "VALUES ('Modern', '', 'already-here', '', 1, :now, :now)"
                ),
                {"now": now},
            )

        # First call: no-op (no `prompt` column to migrate).
        await _migrate_prompt_to_fragments(engine)

        cols = await _column_names(engine, "personas")
        assert "prompt" not in cols
        assert {"soul", "identity", "agents"} <= cols

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT soul, identity, agents "
                        "FROM personas WHERE name = 'Modern'"
                    )
                )
            ).one()
        assert row.soul == ""
        assert row.identity == "already-here"
        assert row.agents == ""

        # Second call: still no-op.
        await _migrate_prompt_to_fragments(engine)

        async with engine.connect() as conn:
            row2 = (
                await conn.execute(
                    text(
                        "SELECT soul, identity, agents "
                        "FROM personas WHERE name = 'Modern'"
                    )
                )
            ).one()
        assert row2.identity == "already-here"
    finally:
        await engine.dispose()
