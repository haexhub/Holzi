"""Tests for `_migrate_prompt_to_fragments` (Plan 36).

The migration helper is one-shot: the first boot after the schema split
brings the `personas` table from the legacy single-`prompt`-column shape
up to the new `soul`/`identity`/`agents` shape, copies the legacy prompt
into `identity`, writes a baseline `persona_history` row per migrated
persona, and drops the old column. On subsequent boots (and on fresh
DBs) it must be a no-op.

The legacy-path test exercises the REAL production flow: a pre-Plan-36
`personas` table + a freshly-`metadata.create_all`-created
`persona_history` table, *without* manually pre-adding the new fragment
columns. That's the gap the fix closes — `metadata.create_all` only
issues `CREATE TABLE IF NOT EXISTS` and does NOT alter the legacy
`personas` table, so the helper itself must do the column adds.
"""
import json
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from hermes.db import init_db
from hermes.personas import _migrate_prompt_to_fragments


async def _column_names(engine, table: str) -> set[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
    return {row.name for row in rows}


async def _create_persona_history_table(engine) -> None:
    """Mirror the `persona_history` shape from `schema.py` via raw SQL.

    Using raw SQL (vs. importing `metadata` + `create_all`) keeps the
    tests independent of the rest of the schema and makes the contract
    visible inline.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE persona_history ("
                "  id INTEGER PRIMARY KEY,"
                "  persona_id INTEGER NOT NULL REFERENCES personas(id)"
                "    ON DELETE CASCADE,"
                "  author TEXT NOT NULL DEFAULT 'user',"
                "  snapshot_json TEXT NOT NULL,"
                "  created_at INTEGER NOT NULL"
                ")"
            )
        )


@pytest.mark.asyncio
async def test_migrate_copies_prompt_to_identity_and_drops_column(
    tmp_path,
) -> None:
    """Production-shaped legacy DB: pre-Plan-36 personas table + an
    empty persona_history table. The helper must add the three fragment
    columns itself, copy `prompt` into `identity`, write a baseline
    history row, and drop `prompt`.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pre-plan36.db'}"
    )
    try:
        # Build a pre-Plan-36 `personas` table by hand (old `prompt`
        # column, no soul/identity/agents). This is exactly the shape a
        # legacy DB has when the new code first boots against it.
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

        # `persona_history` is a NEW table — `metadata.create_all` creates
        # it on every boot (legacy DBs too, since CREATE TABLE IF NOT
        # EXISTS works for new tables). Simulate that here.
        await _create_persona_history_table(engine)

        # Sanity: old `personas` shape in place, the fragment columns
        # are NOT here yet — the helper must add them.
        cols_before = await _column_names(engine, "personas")
        assert "prompt" in cols_before
        assert "soul" not in cols_before
        assert "identity" not in cols_before
        assert "agents" not in cols_before

        # Run the migration exactly as lifespan does — no manual
        # ALTER TABLE first.
        await _migrate_prompt_to_fragments(engine)

        cols_after = await _column_names(engine, "personas")
        assert "prompt" not in cols_after
        assert {"soul", "identity", "agents"} <= cols_after

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, name, soul, identity, agents "
                        "FROM personas WHERE name = 'Legacy'"
                    )
                )
            ).one()
        assert row.soul == ""
        assert row.identity == "legacy-text"
        assert row.agents == ""

        # Baseline history row: exactly one row for this persona,
        # author='migration', snapshot matches the post-migration state.
        async with engine.connect() as conn:
            hist_rows = (
                await conn.execute(
                    text(
                        "SELECT persona_id, author, snapshot_json "
                        "FROM persona_history WHERE persona_id = :pid"
                    ),
                    {"pid": row.id},
                )
            ).all()
        assert len(hist_rows) == 1
        hist = hist_rows[0]
        assert hist.author == "migration"
        snap = json.loads(hist.snapshot_json)
        assert snap == {
            "name": "Legacy",
            "soul": "",
            "identity": "legacy-text",
            "agents": "",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migrate_via_full_init_db_path(tmp_path) -> None:
    """End-to-end production path: seed a legacy `personas` table in a
    file, then call the real `init_db` (which runs `metadata.create_all`)
    followed by `_migrate_prompt_to_fragments`. This locks in the
    contract that `create_all` does NOT add the fragment columns to the
    existing `personas` table — the helper itself must do it — and that
    the lifespan order (`init_db` → migration) works against a real
    legacy DB without manual ALTER intervention.
    """
    db_path = tmp_path / "legacy.db"
    # Seed a pre-Plan-36 `personas` table in the file before `init_db`
    # ever touches it. Use a separate engine so the DDL commits to disk
    # before we dispose it.
    seed_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with seed_engine.begin() as conn:
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
                    "VALUES ('Alpha', 'prompt-alpha', 1, :now, :now), "
                    "('Beta', 'prompt-beta', 0, :now, :now)"
                ),
                {"now": now},
            )
    finally:
        await seed_engine.dispose()

    # Now hand the legacy file to the real init_db — this proves that
    # `metadata.create_all` alone does NOT add the fragment columns.
    engine = await init_db(str(db_path))
    try:
        # Sanity: create_all created `persona_history` (new table) but
        # left `personas` with its legacy shape — the very gap the
        # migration helper closes.
        cols_after_create_all = await _column_names(engine, "personas")
        assert "prompt" in cols_after_create_all
        assert "soul" not in cols_after_create_all
        assert "identity" not in cols_after_create_all
        assert "agents" not in cols_after_create_all
        async with engine.connect() as conn:
            tables = {
                row.name
                for row in (
                    await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).all()
            }
        assert "persona_history" in tables

        await _migrate_prompt_to_fragments(engine)

        cols_final = await _column_names(engine, "personas")
        assert "prompt" not in cols_final
        assert {"soul", "identity", "agents"} <= cols_final

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, name, soul, identity, agents "
                        "FROM personas ORDER BY name"
                    )
                )
            ).all()
        assert [r.name for r in rows] == ["Alpha", "Beta"]
        assert rows[0].identity == "prompt-alpha"
        assert rows[1].identity == "prompt-beta"
        assert all(r.soul == "" and r.agents == "" for r in rows)

        # One baseline history row per migrated persona.
        async with engine.connect() as conn:
            hist = (
                await conn.execute(
                    text(
                        "SELECT persona_id, author, snapshot_json "
                        "FROM persona_history ORDER BY persona_id"
                    )
                )
            ).all()
        assert len(hist) == 2
        assert all(h.author == "migration" for h in hist)
        snaps = sorted(
            (json.loads(h.snapshot_json) for h in hist),
            key=lambda s: s["name"],
        )
        assert snaps[0] == {
            "name": "Alpha",
            "soul": "",
            "identity": "prompt-alpha",
            "agents": "",
        }
        assert snaps[1] == {
            "name": "Beta",
            "soul": "",
            "identity": "prompt-beta",
            "agents": "",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migrate_idempotent_on_already_migrated_db(tmp_path) -> None:
    """Post-migration DB: helper must be a pure no-op — table shape
    unchanged, no extra `persona_history` rows.
    """
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

        await _create_persona_history_table(engine)

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

        # The no-op branch must NOT touch `persona_history` either.
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT COUNT(*) AS c FROM persona_history")
                )
            ).one()
        assert count.c == 0

        # Second call: still no-op, still no history row.
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

        async with engine.connect() as conn:
            count2 = (
                await conn.execute(
                    text("SELECT COUNT(*) AS c FROM persona_history")
                )
            ).one()
        assert count2.c == 0
    finally:
        await engine.dispose()
