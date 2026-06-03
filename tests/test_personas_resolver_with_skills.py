"""Resolver-with-Skills tests (Plan 33).

Plan 29-A's resolver composed `persona + capability_index + channel`;
Plan 33 inserts active skills between persona and capability_index so
the composition becomes `persona + skills + capability_index + channel`
(empty parts are skipped, the existing test suite stays green).
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import capabilities
from hermes.personas import (
    CHANNEL_REGISTRY,
    DEFAULT_PERSONA_PROMPT,
    ensure_backfill,
    get_effective_system_prompt,
)
from hermes.repository import personas as personas_repo
from hermes.repository import skills as skills_repo


@pytest.mark.asyncio
async def test_no_skills_keeps_existing_composition(conn: AsyncEngine) -> None:
    """Persona with zero skills produces the same prompt as before Plan 33."""
    await ensure_backfill(conn)
    prompt = await get_effective_system_prompt("web", conn)
    expected = (
        f"{DEFAULT_PERSONA_PROMPT}\n\n"
        f"{CHANNEL_REGISTRY['web']['default_prompt']}"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_single_active_skill_is_injected(conn: AsyncEngine) -> None:
    await ensure_backfill(conn)
    default = await personas_repo.get_default(conn)
    assert default is not None
    s = await skills_repo.create(
        conn,
        slug="strict-german",
        name="Strict German",
        description="German only",
        when_to_use=None,
        body_markdown="Antworte ausschließlich auf Deutsch.",
    )
    await skills_repo.set_persona_skills(
        conn,
        default.id,
        [{"skill_id": s.id, "ordering": 0, "enabled": True}],
    )

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        f"{DEFAULT_PERSONA_PROMPT}\n\n"
        f"Antworte ausschließlich auf Deutsch.\n\n"
        f"{CHANNEL_REGISTRY['web']['default_prompt']}"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_skills_respect_ordering(conn: AsyncEngine) -> None:
    await ensure_backfill(conn)
    default = await personas_repo.get_default(conn)
    assert default is not None
    a = await skills_repo.create(
        conn,
        slug="a",
        name="A",
        description="a",
        when_to_use=None,
        body_markdown="SKILL_A_BODY",
    )
    b = await skills_repo.create(
        conn,
        slug="b",
        name="B",
        description="b",
        when_to_use=None,
        body_markdown="SKILL_B_BODY",
    )
    # Insert B first (ordering 0), then A — composition order follows
    # `ordering`, not insertion order.
    await skills_repo.set_persona_skills(
        conn,
        default.id,
        [
            {"skill_id": b.id, "ordering": 0, "enabled": True},
            {"skill_id": a.id, "ordering": 1, "enabled": True},
        ],
    )

    prompt = await get_effective_system_prompt("web", conn)

    assert "SKILL_B_BODY\n\nSKILL_A_BODY" in prompt


@pytest.mark.asyncio
async def test_disabled_skill_is_skipped(conn: AsyncEngine) -> None:
    await ensure_backfill(conn)
    default = await personas_repo.get_default(conn)
    assert default is not None
    a = await skills_repo.create(
        conn,
        slug="a",
        name="A",
        description="a",
        when_to_use=None,
        body_markdown="SKILL_A_BODY",
    )
    b = await skills_repo.create(
        conn,
        slug="b",
        name="B",
        description="b",
        when_to_use=None,
        body_markdown="SKILL_B_BODY",
    )
    await skills_repo.set_persona_skills(
        conn,
        default.id,
        [
            {"skill_id": a.id, "ordering": 0, "enabled": True},
            {"skill_id": b.id, "ordering": 1, "enabled": False},
        ],
    )

    prompt = await get_effective_system_prompt("web", conn)

    assert "SKILL_A_BODY" in prompt
    assert "SKILL_B_BODY" not in prompt


@pytest.mark.asyncio
async def test_skills_attach_to_resolved_persona_not_default(
    conn: AsyncEngine,
) -> None:
    """When a channel pins a specific persona, that persona's skills win."""
    await ensure_backfill(conn)
    default = await personas_repo.get_default(conn)
    assert default is not None

    custom = await personas_repo.create(
        conn, name="Custom", prompt="CUSTOM_PERSONA", is_default=False
    )

    from hermes.repository import channels as channels_repo

    await channels_repo.update(conn, "task", default_persona_id=custom.id)

    default_skill = await skills_repo.create(
        conn,
        slug="default-skill",
        name="Default Skill",
        description="d",
        when_to_use=None,
        body_markdown="DEFAULT_SKILL_BODY",
    )
    custom_skill = await skills_repo.create(
        conn,
        slug="custom-skill",
        name="Custom Skill",
        description="c",
        when_to_use=None,
        body_markdown="CUSTOM_SKILL_BODY",
    )
    await skills_repo.set_persona_skills(
        conn,
        default.id,
        [{"skill_id": default_skill.id, "ordering": 0, "enabled": True}],
    )
    await skills_repo.set_persona_skills(
        conn,
        custom.id,
        [{"skill_id": custom_skill.id, "ordering": 0, "enabled": True}],
    )

    prompt = await get_effective_system_prompt("task", conn)

    assert "CUSTOM_SKILL_BODY" in prompt
    assert "DEFAULT_SKILL_BODY" not in prompt


@pytest.mark.asyncio
async def test_skills_compose_with_capability_index(
    conn: AsyncEngine, monkeypatch
) -> None:
    """Skills sit between persona and capability index; index still injects."""
    monkeypatch.setattr(
        capabilities, "load_capability_index", lambda: "INDEX-MARKER"
    )
    await ensure_backfill(conn)
    default = await personas_repo.get_default(conn)
    assert default is not None
    s = await skills_repo.create(
        conn,
        slug="s",
        name="S",
        description="s",
        when_to_use=None,
        body_markdown="SKILL_BODY",
    )
    await skills_repo.set_persona_skills(
        conn,
        default.id,
        [{"skill_id": s.id, "ordering": 0, "enabled": True}],
    )

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        f"{DEFAULT_PERSONA_PROMPT}\n\n"
        f"SKILL_BODY\n\n"
        f"INDEX-MARKER\n\n"
        f"{CHANNEL_REGISTRY['web']['default_prompt']}"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_all_disabled_skills_collapse_to_empty(conn: AsyncEngine) -> None:
    """Persona with only disabled skills behaves like persona with no skills."""
    await ensure_backfill(conn)
    default = await personas_repo.get_default(conn)
    assert default is not None
    s = await skills_repo.create(
        conn,
        slug="s",
        name="S",
        description="s",
        when_to_use=None,
        body_markdown="SKILL_BODY",
    )
    await skills_repo.set_persona_skills(
        conn,
        default.id,
        [{"skill_id": s.id, "ordering": 0, "enabled": False}],
    )

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        f"{DEFAULT_PERSONA_PROMPT}\n\n"
        f"{CHANNEL_REGISTRY['web']['default_prompt']}"
    )
    assert prompt == expected
    assert "SKILL_BODY" not in prompt
