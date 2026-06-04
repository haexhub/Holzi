"""Unit tests for the effective-system-prompt resolver (Plan 29-A → 36).

Plan 36 splits the single `persona.prompt` column into three labelled
fragments (`soul`, `identity`, `agents`) and renders them with Markdown
H2 headers. These tests pin the exact composition contract — see the
docstring of ``get_effective_system_prompt`` for the spec.

The tests deliberately set up personas + channels directly via the repo
layer instead of going through ``ensure_backfill``, which still seeds
the old single-prompt shape until Plan-36 Task 5 reshapes it.
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import capabilities
from hermes.personas import (
    CHANNEL_REGISTRY,
    get_effective_system_prompt,
)
from hermes.repository import channels as channels_repo
from hermes.repository import personas as personas_repo
from hermes.repository import skills as skills_repo


async def _seed_default_persona(
    engine: AsyncEngine,
    *,
    soul: str = "",
    identity: str = "",
    agents: str = "",
    name: str = "Hermes",
):
    """Helper: seed channels + a single default persona with the given
    fragments. Returns the created persona row.
    """
    await channels_repo.ensure_seeded(engine)
    return await personas_repo.create(
        engine,
        name=name,
        soul=soul,
        identity=identity,
        agents=agents,
        is_default=True,
    )


@pytest.mark.asyncio
async def test_unknown_channel_raises_key_error(conn: AsyncEngine) -> None:
    await _seed_default_persona(conn, identity="anything")
    with pytest.raises(KeyError):
        await get_effective_system_prompt("discord", conn)


@pytest.mark.asyncio
async def test_composition_full_persona(conn: AsyncEngine) -> None:
    """Persona with all three non-empty fragments + custom channel prompt
    composes Soul → Identity → Agents → channel, each separated by a
    single blank line. Exact string match — this pins the wire format.
    """
    await _seed_default_persona(
        conn,
        soul="be direct",
        identity="you are Hermes",
        agents="ask before destructive actions",
    )
    await channels_repo.update(conn, "web", prompt="web channel prompt")

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        "## Soul\nbe direct\n\n"
        "## Identity\nyou are Hermes\n\n"
        "## Agents\nask before destructive actions\n\n"
        "web channel prompt"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_composition_skips_empty_section(conn: AsyncEngine) -> None:
    """`soul=""` and `agents=""` → only Identity is rendered; no empty
    headers, no double separator."""
    await _seed_default_persona(conn, soul="", identity="x", agents="")
    await channels_repo.update(conn, "web", prompt="C")

    prompt = await get_effective_system_prompt("web", conn)

    assert prompt == "## Identity\nx\n\nC"


@pytest.mark.asyncio
async def test_composition_skips_whitespace_only_section(
    conn: AsyncEngine,
) -> None:
    """A whitespace-only body counts as empty after `.strip()` — header
    and body are both dropped from the output."""
    await _seed_default_persona(
        conn, soul="   \n  ", identity="body", agents="\t"
    )
    await channels_repo.update(conn, "web", prompt="C")

    prompt = await get_effective_system_prompt("web", conn)

    assert prompt == "## Identity\nbody\n\nC"


@pytest.mark.asyncio
async def test_composition_section_order_stable(conn: AsyncEngine) -> None:
    """Even if the dataclass is constructed with kwargs in a different
    order, the resolver always renders Soul → Identity → Agents."""
    await channels_repo.ensure_seeded(conn)
    # Construct with kwargs in a non-canonical order to make the point.
    await personas_repo.create(
        conn,
        name="Mixed",
        agents="A-body",
        identity="I-body",
        soul="S-body",
        is_default=True,
    )
    await channels_repo.update(conn, "web", prompt="C")

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        "## Soul\nS-body\n\n"
        "## Identity\nI-body\n\n"
        "## Agents\nA-body\n\n"
        "C"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_composition_skills_between_persona_and_channel(
    conn: AsyncEngine,
) -> None:
    """Skills block sits between the persona-section group and the
    channel prompt; capability_index goes after skills."""
    persona = await _seed_default_persona(
        conn, identity="I-body"
    )
    await channels_repo.update(conn, "web", prompt="C")

    skill_one = await skills_repo.create(
        conn,
        slug="alpha",
        name="Alpha",
        description="d",
        when_to_use=None,
        body_markdown="ALPHA-BODY",
    )
    skill_two = await skills_repo.create(
        conn,
        slug="beta",
        name="Beta",
        description="d",
        when_to_use=None,
        body_markdown="BETA-BODY",
    )
    await skills_repo.set_persona_skills(
        conn,
        persona.id,
        [
            {"skill_id": skill_one.id, "ordering": 0, "enabled": True},
            {"skill_id": skill_two.id, "ordering": 1, "enabled": True},
        ],
    )

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        "## Identity\nI-body\n\n"
        "ALPHA-BODY\n\n"
        "BETA-BODY\n\n"
        "C"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_composition_only_channel_when_persona_empty(
    conn: AsyncEngine,
) -> None:
    """Persona with all three sections empty contributes nothing — the
    output is the channel prompt verbatim, no leading whitespace."""
    await _seed_default_persona(conn, soul="", identity="", agents="")
    await channels_repo.update(conn, "web", prompt="just-the-channel")

    prompt = await get_effective_system_prompt("web", conn)

    assert prompt == "just-the-channel"


@pytest.mark.asyncio
async def test_composition_falls_back_to_default_persona_when_channel_persona_unset(
    conn: AsyncEngine,
) -> None:
    """When the channel row's `default_persona_id` is NULL, the resolver
    falls back to the globally-default persona (`is_default = 1`)."""
    await channels_repo.ensure_seeded(conn)
    # Non-default persona that should NOT win.
    await personas_repo.create(
        conn,
        name="Other",
        soul="",
        identity="not-me",
        agents="",
        is_default=False,
    )
    # Globally-default persona that should win.
    await personas_repo.create(
        conn,
        name="Default",
        soul="",
        identity="default-identity",
        agents="",
        is_default=True,
    )
    await channels_repo.update(conn, "web", prompt="C")

    prompt = await get_effective_system_prompt("web", conn)

    assert prompt == "## Identity\ndefault-identity\n\nC"


@pytest.mark.asyncio
async def test_channel_specific_persona_wins_over_global_default(
    conn: AsyncEngine,
) -> None:
    """`channel_prompts.default_persona_id` overrides the global default."""
    await channels_repo.ensure_seeded(conn)
    await personas_repo.create(
        conn,
        name="Global",
        soul="",
        identity="global-identity",
        agents="",
        is_default=True,
    )
    custom = await personas_repo.create(
        conn,
        name="Strict Reviewer",
        soul="",
        identity="Be merciless about types.",
        agents="",
        is_default=False,
    )
    await channels_repo.update(conn, "task", default_persona_id=custom.id)

    prompt = await get_effective_system_prompt("task", conn)

    expected = (
        "## Identity\nBe merciless about types.\n\n"
        f"{CHANNEL_REGISTRY['task']['default_prompt']}"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_capability_index_is_injected_between_skills_and_channel(
    conn: AsyncEngine, monkeypatch
) -> None:
    """Capability index sits between skills (if any) and the channel
    prompt — and after the persona-section group."""
    monkeypatch.setattr(
        capabilities, "load_capability_index", lambda: "INDEX-MARKER"
    )
    await _seed_default_persona(conn, identity="I-body")
    await channels_repo.update(conn, "web", prompt="C")

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        "## Identity\nI-body\n\n"
        "INDEX-MARKER\n\n"
        "C"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_empty_capability_index_is_omitted_from_composition(
    conn: AsyncEngine, monkeypatch
) -> None:
    """Pinned explicitly even though the autouse fixture already returns
    "" — the empty-index branch shouldn't introduce a stray separator."""
    monkeypatch.setattr(capabilities, "load_capability_index", lambda: "")
    await _seed_default_persona(conn, identity="I-body")
    await channels_repo.update(conn, "web", prompt="C")

    prompt = await get_effective_system_prompt("web", conn)

    assert prompt == "## Identity\nI-body\n\nC"
    assert "\n\n\n" not in prompt
