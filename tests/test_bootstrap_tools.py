"""Tests for persona_update + mark_bootstrap_complete tools (Plan 37 Task 5)."""
import json
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import ensure_backfill
from hermes.repository import persona_history as history_repo
from hermes.repository import personas as personas_repo
from hermes.tools.bootstrap import build_bootstrap_tools
from hermes.users import ensure_users_seeded


async def _setup(engine: AsyncEngine) -> None:
    """Seed the default persona and users row."""
    await ensure_backfill(engine)
    await ensure_users_seeded(engine)


# ---------------------------------------------------------------------------
# persona_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_update_writes_three_fragments(conn: AsyncEngine) -> None:
    """persona_update writes soul/identity/agents to the default persona."""
    await _setup(conn)
    tools = build_bootstrap_tools(conn)
    persona_update = next(t for t in tools if t.name == "persona_update")

    result = await persona_update.handler(
        {
            "soul": "be direct",
            "identity": "I am Max",
            "agents": "focus on coding",
        }
    )
    data = json.loads(result)

    assert data["soul"] == "be direct"
    assert data["identity"] == "I am Max"
    assert data["agents"] == "focus on coding"

    # Verify DB state
    persona = await personas_repo.get_default(conn)
    assert persona is not None
    assert persona.soul == "be direct"
    assert persona.identity == "I am Max"
    assert persona.agents == "focus on coding"


@pytest.mark.asyncio
async def test_persona_update_writes_history_row_with_bootstrap_author(
    conn: AsyncEngine,
) -> None:
    """persona_update writes a persona_history row with author='bootstrap'."""
    await _setup(conn)
    tools = build_bootstrap_tools(conn)
    persona_update = next(t for t in tools if t.name == "persona_update")

    await persona_update.handler(
        {"soul": "be direct", "identity": "I am Max", "agents": "coding"}
    )

    persona = await personas_repo.get_default(conn)
    assert persona is not None
    history = await history_repo.list_for_persona(conn, persona.id)

    # Should have at least one history row from the update
    bootstrap_rows = [h for h in history if h.author == "bootstrap"]
    assert len(bootstrap_rows) >= 1


@pytest.mark.asyncio
async def test_persona_update_all_none_returns_422(conn: AsyncEngine) -> None:
    """All-None args → 422 PERSONA_FRAGMENTS_ALL_EMPTY."""
    await _setup(conn)
    tools = build_bootstrap_tools(conn)
    persona_update = next(t for t in tools if t.name == "persona_update")

    with pytest.raises(HTTPException) as exc_info:
        await persona_update.handler({})
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "PERSONA_FRAGMENTS_ALL_EMPTY"


# ---------------------------------------------------------------------------
# mark_bootstrap_complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_bootstrap_complete_flips_flag(conn: AsyncEngine) -> None:
    """mark_bootstrap_complete sets bootstrap_completed=1."""
    await _setup(conn)
    tools = build_bootstrap_tools(conn)
    mark_complete = next(t for t in tools if t.name == "mark_bootstrap_complete")

    result = await mark_complete.handler({})
    data = json.loads(result)
    assert data == {"ok": True}

    async with conn.connect() as c:
        row = (
            await c.execute(
                text("SELECT bootstrap_completed FROM users WHERE id = 1")
            )
        ).first()
    assert row is not None
    assert row.bootstrap_completed == 1


@pytest.mark.asyncio
async def test_mark_bootstrap_complete_is_idempotent(conn: AsyncEngine) -> None:
    """Calling mark_bootstrap_complete twice is harmless."""
    await _setup(conn)
    tools = build_bootstrap_tools(conn)
    mark_complete = next(t for t in tools if t.name == "mark_bootstrap_complete")

    await mark_complete.handler({})
    result = await mark_complete.handler({})  # second call
    data = json.loads(result)
    assert data == {"ok": True}

    async with conn.connect() as c:
        row = (
            await c.execute(
                text("SELECT bootstrap_completed FROM users WHERE id = 1")
            )
        ).first()
    assert row is not None
    assert row.bootstrap_completed == 1
