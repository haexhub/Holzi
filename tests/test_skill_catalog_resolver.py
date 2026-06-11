"""Tests for Plan 37 resolver additions: catalog index + bootstrap hint.

Pins the exact output format for `## Available skills` and the
bootstrap hint — tests fail fast if the resolver format drifts.
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import capabilities
from hermes import users as users_mod
from hermes.personas import (
    ensure_bootstrap_skill_seeded,
    get_effective_system_prompt,
)
from hermes.repository import channels as channels_repo
from hermes.repository import personas as personas_repo
from hermes.repository import skills as skills_repo


async def _seed_env(engine: AsyncEngine, *, channel_prompt: str = "C") -> None:
    """Seed a default persona + channels. Skills seeded per-test."""
    await channels_repo.ensure_seeded(engine)
    await personas_repo.create(
        engine,
        user_id=1,
        name="Hermes",
        soul="",
        identity="I-body",
        agents="",
        is_default=True,
    )
    await channels_repo.update(engine, "web", prompt=channel_prompt)


# ---------------------------------------------------------------------------
# Catalog index format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_index_single_skill_with_when_to_use(
    conn: AsyncEngine,
) -> None:
    """One enabled skill → catalog section with correct format."""
    await _seed_env(conn)
    await skills_repo.create(
        conn,
        slug="my-skill",
        name="My Skill",
        description="Does things",
        when_to_use="When you need things done",
        body_markdown="BODY",
    )
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    expected_line = "- my-skill — Does things (use when: When you need things done)"
    assert "## Available skills" in prompt
    assert expected_line in prompt


@pytest.mark.asyncio
async def test_catalog_index_empty_when_to_use_omits_suffix(
    conn: AsyncEngine,
) -> None:
    """Skill with empty when_to_use → no `(use when: ...)` suffix."""
    await _seed_env(conn)
    await skills_repo.create(
        conn,
        slug="plain-skill",
        name="Plain",
        description="A plain skill",
        when_to_use="",
        body_markdown="BODY",
    )
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    assert "- plain-skill — A plain skill" in prompt
    assert "use when:" not in prompt


@pytest.mark.asyncio
async def test_catalog_index_disabled_skill_not_shown(
    conn: AsyncEngine,
) -> None:
    """Disabled skills do NOT appear in the catalog index."""
    await _seed_env(conn)
    await skills_repo.create(
        conn,
        slug="hidden",
        name="Hidden",
        description="Should not appear",
        when_to_use="",
        body_markdown="BODY",
        enabled=False,
    )
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    assert "## Available skills" not in prompt
    assert "hidden" not in prompt


@pytest.mark.asyncio
async def test_catalog_index_no_skills_omits_section(
    conn: AsyncEngine,
) -> None:
    """Zero enabled skills → no `## Available skills` section at all."""
    await _seed_env(conn)
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    assert "## Available skills" not in prompt


@pytest.mark.asyncio
async def test_catalog_index_alphabetical_order(
    conn: AsyncEngine,
) -> None:
    """Multiple skills are listed alphabetically by slug."""
    await _seed_env(conn)
    await skills_repo.create(
        conn, slug="zzz", name="ZZZ", description="z-desc", when_to_use="", body_markdown="Z"
    )
    await skills_repo.create(
        conn, slug="aaa", name="AAA", description="a-desc", when_to_use="", body_markdown="A"
    )
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    aaa_pos = prompt.index("- aaa")
    zzz_pos = prompt.index("- zzz")
    assert aaa_pos < zzz_pos


# ---------------------------------------------------------------------------
# Section order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_section_order_persona_catalog_capability_channel(
    conn: AsyncEngine, monkeypatch
) -> None:
    """Order: Persona sections → Catalog → capability_index → channel_prompt."""
    monkeypatch.setattr(capabilities, "load_capability_index", lambda: "CAP-INDEX")
    await _seed_env(conn, channel_prompt="CHANNEL")
    await skills_repo.create(
        conn,
        slug="my-skill",
        name="My",
        description="desc",
        when_to_use="when",
        body_markdown="BODY",
    )
    prompt = await get_effective_system_prompt("web", conn, user_id=1)

    persona_pos = prompt.index("## Identity")
    catalog_pos = prompt.index("## Available skills")
    cap_pos = prompt.index("CAP-INDEX")
    channel_pos = prompt.index("CHANNEL")

    assert persona_pos < catalog_pos < cap_pos < channel_pos


# ---------------------------------------------------------------------------
# Bootstrap hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_hint_when_not_completed(conn: AsyncEngine) -> None:
    """Bootstrap hint appears when bootstrap_completed = 0 and the
    bootstrap skill is seeded + enabled."""
    await _seed_env(conn)
    await users_mod.ensure_users_seeded(conn)
    await ensure_bootstrap_skill_seeded(conn)
    # Default after seed is bootstrap_completed=0
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    assert "You haven't been set up yet." in prompt
    assert "skill_load('bootstrap-first-chat')" in prompt


@pytest.mark.asyncio
async def test_bootstrap_hint_absent_when_completed(conn: AsyncEngine) -> None:
    """Bootstrap hint is omitted when bootstrap_completed = 1."""
    from sqlalchemy import text
    await _seed_env(conn)
    await users_mod.ensure_users_seeded(conn)
    await ensure_bootstrap_skill_seeded(conn)
    async with conn.begin() as txn:
        await txn.execute(text("UPDATE users SET bootstrap_completed = 1 WHERE id = 1"))
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    assert "You haven't been set up yet." not in prompt


@pytest.mark.asyncio
async def test_bootstrap_hint_after_channel_prompt(conn: AsyncEngine) -> None:
    """Bootstrap hint comes AFTER the channel prompt."""
    await _seed_env(conn, channel_prompt="CHANNEL-PROMPT")
    await users_mod.ensure_users_seeded(conn)
    await ensure_bootstrap_skill_seeded(conn)
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    channel_pos = prompt.index("CHANNEL-PROMPT")
    hint_pos = prompt.index("You haven't been set up yet.")
    assert channel_pos < hint_pos


@pytest.mark.asyncio
async def test_bootstrap_hint_absent_when_skill_missing(
    conn: AsyncEngine,
) -> None:
    """Hint is suppressed when the bootstrap-first-chat skill row does
    not exist — otherwise the agent would follow the hint and hit a 404
    on `skill_load`."""
    await _seed_env(conn)
    await users_mod.ensure_users_seeded(conn)
    # Deliberately do NOT seed the bootstrap skill.
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    assert "You haven't been set up yet." not in prompt


@pytest.mark.asyncio
async def test_bootstrap_hint_absent_when_skill_disabled(
    conn: AsyncEngine,
) -> None:
    """Hint is suppressed when the bootstrap-first-chat skill exists
    but the user has disabled it — `skill_load` would 404 the agent."""
    from sqlalchemy import text
    await _seed_env(conn)
    await users_mod.ensure_users_seeded(conn)
    await ensure_bootstrap_skill_seeded(conn)
    async with conn.begin() as txn:
        await txn.execute(
            text("UPDATE skills SET enabled = 0 WHERE slug = 'bootstrap-first-chat'")
        )
    prompt = await get_effective_system_prompt("web", conn, user_id=1)
    assert "You haven't been set up yet." not in prompt
