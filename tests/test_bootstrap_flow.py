"""Mini-integration test for the full bootstrap flow (Plan 37 Task 8).

Tests the end-to-end scenario: fresh DB → resolver has bootstrap hint →
simulate agent tool calls → verify final DB state + hint gone.
No LLM calls are made; tool handlers are invoked directly.
"""
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import (
    _BOOTSTRAP_HINT,
    ensure_backfill,
    ensure_bootstrap_skill_seeded,
    get_effective_system_prompt,
)
from hermes.repository import persona_history as history_repo
from hermes.repository import personas as personas_repo
from hermes.tools.bootstrap import build_bootstrap_tools
from hermes.tools.skills import build_skill_tools
from hermes.users import ensure_users_seeded, is_bootstrap_completed


async def _boot(engine: AsyncEngine) -> None:
    """Simulate full lifespan boot sequence."""
    await ensure_backfill(engine)
    await ensure_users_seeded(engine)
    await ensure_bootstrap_skill_seeded(engine)


@pytest.mark.asyncio
async def test_bootstrap_flow_end_to_end(conn: AsyncEngine) -> None:
    """Full bootstrap flow: hint present → skill_load → persona_update
    → mark_complete → hint gone + DB state correct."""
    await _boot(conn)

    # 1. Fresh DB: resolver output contains bootstrap hint
    prompt_before = await get_effective_system_prompt("web", conn)
    assert _BOOTSTRAP_HINT in prompt_before, "Bootstrap hint should be in fresh prompt"

    # 2. Simulate agent: skill_load('bootstrap-first-chat')
    skill_tools = build_skill_tools(conn)
    skill_load = next(t for t in skill_tools if t.name == "skill_load")
    skill_result = await skill_load.handler({"slug": "bootstrap-first-chat"})
    skill_data = json.loads(skill_result)
    assert skill_data["slug"] == "bootstrap-first-chat"
    assert skill_data["body_markdown"]  # non-empty body

    # 3. Simulate agent: persona_update with Q&A results
    bootstrap_tools = build_bootstrap_tools(conn)
    persona_update = next(t for t in bootstrap_tools if t.name == "persona_update")
    update_result = await persona_update.handler(
        {
            "soul": "direkt",
            "identity": "Max Muster",
            "agents": "coding",
        }
    )
    update_data = json.loads(update_result)
    assert update_data["soul"] == "direkt"
    assert update_data["identity"] == "Max Muster"
    assert update_data["agents"] == "coding"

    # 4. Simulate agent: mark_bootstrap_complete()
    mark_complete = next(t for t in bootstrap_tools if t.name == "mark_bootstrap_complete")
    complete_result = await mark_complete.handler({})
    assert json.loads(complete_result) == {"ok": True}

    # 5. Verify final DB state: bootstrap_completed = 1
    assert await is_bootstrap_completed(conn) is True

    # 6. Verify default persona has updated fragments
    persona = await personas_repo.get_default(conn)
    assert persona is not None
    assert persona.soul == "direkt"
    assert persona.identity == "Max Muster"
    assert persona.agents == "coding"

    # 7. Verify persona_history has a 'bootstrap' author row
    history = await history_repo.list_for_persona(conn, persona.id)
    bootstrap_rows = [h for h in history if h.author == "bootstrap"]
    assert len(bootstrap_rows) >= 1

    # 8. Verify resolver output NO LONGER contains bootstrap hint
    prompt_after = await get_effective_system_prompt("web", conn)
    assert _BOOTSTRAP_HINT not in prompt_after, "Bootstrap hint should be gone after completion"
