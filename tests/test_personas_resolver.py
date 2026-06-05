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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes import capabilities
from hermes.crypto import EncryptedBlob
from hermes.personas import (
    CHANNEL_REGISTRY,
    PersonaContext,
    get_effective_system_prompt,
    resolve_persona_context,
)
from hermes.repository import channels as channels_repo
from hermes.repository import llm_credentials as cred_repo
from hermes.repository import personas as personas_repo
from hermes.repository import skills as skills_repo


async def _seed_default_persona(
    engine: AsyncEngine,
    *,
    soul: str = "",
    identity: str = "",
    agents: str = "",
    name: str = "Hermes",
    bootstrap_completed: int = 1,
):
    """Helper: seed channels + a single default persona with the given
    fragments. Also seeds the users row so the bootstrap hint is
    controlled. Returns the created persona row.
    """
    import time
    await channels_repo.ensure_seeded(engine)
    persona = await personas_repo.create(
        engine,
        name=name,
        soul=soul,
        identity=identity,
        agents=agents,
        is_default=True,
    )
    async with engine.begin() as txn:
        await txn.execute(
            text(
                "INSERT OR IGNORE INTO users(id, bootstrap_completed, created_at) "
                "VALUES (1, :bc, :ts)"
            ),
            {"bc": bootstrap_completed, "ts": int(time.time())},
        )
    return persona


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
    # Use the helper so the users row is seeded (bootstrap_completed=1)
    # and the bootstrap hint doesn't pollute the exact-match assertion.
    await _seed_default_persona(
        conn,
        name="Mixed",
        agents="A-body",
        identity="I-body",
        soul="S-body",
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
async def test_composition_catalog_between_persona_and_channel(
    conn: AsyncEngine,
) -> None:
    """Catalog index sits between the persona-section group and the
    channel prompt; capability_index goes after the catalog.

    Plan 37: skills are shown as a catalog index (slug + description),
    not as inlined bodies. Two enabled skills produce two catalog lines.
    """
    await _seed_default_persona(conn, identity="I-body")
    await channels_repo.update(conn, "web", prompt="C")

    await skills_repo.create(
        conn,
        slug="alpha",
        name="Alpha",
        description="alpha-desc",
        when_to_use="",
        body_markdown="ALPHA-BODY",
    )
    await skills_repo.create(
        conn,
        slug="beta",
        name="Beta",
        description="beta-desc",
        when_to_use="",
        body_markdown="BETA-BODY",
    )

    prompt = await get_effective_system_prompt("web", conn)

    expected = (
        "## Identity\nI-body\n\n"
        "## Available skills\n"
        "- alpha — alpha-desc\n"
        "- beta — beta-desc\n\n"
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
    import time
    await channels_repo.ensure_seeded(conn)
    # Seed users so bootstrap hint is suppressed.
    async with conn.begin() as txn:
        await txn.execute(
            text(
                "INSERT OR IGNORE INTO users(id, bootstrap_completed, created_at) "
                "VALUES (1, 1, :ts)"
            ),
            {"ts": int(time.time())},
        )
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
    import time
    await channels_repo.ensure_seeded(conn)
    # Seed users so bootstrap hint is suppressed.
    async with conn.begin() as txn:
        await txn.execute(
            text(
                "INSERT OR IGNORE INTO users(id, bootstrap_completed, created_at) "
                "VALUES (1, 1, :ts)"
            ),
            {"ts": int(time.time())},
        )
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


# ---------------------------------------------------------------------------
# resolve_persona_context tests (Plan 29-D Task 3)
# ---------------------------------------------------------------------------


async def _seed_credential(
    engine,
    *,
    model: str | None = None,
    is_active: bool = False,
) -> int:
    """Seed a dummy openai api_key credential. Returns credential id."""
    from sqlalchemy import text
    cred = await cred_repo.create_api_key(
        engine,
        provider="openai",
        display_name="test-cred",
        base_url=None,
        ciphertext=EncryptedBlob(iv="aa", tag="bb", data="cc"),
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE llm_credentials SET model=:m, is_active=:a WHERE id=:id"),
            {"m": model, "a": 1 if is_active else 0, "id": cred.id},
        )
    return cred.id


@pytest.mark.asyncio
async def test_resolve_context_uses_active_cred_when_persona_has_none(conn):
    cred_id = await _seed_credential(conn, model="gpt-4o", is_active=True)
    await _seed_default_persona(conn, identity="x")
    ctx = await resolve_persona_context("web", conn)
    assert isinstance(ctx, PersonaContext)
    assert ctx.credential.id == cred_id
    assert ctx.model == "gpt-4o"


@pytest.mark.asyncio
async def test_resolve_context_persona_credential_overrides_active(conn):
    await _seed_credential(conn, model="gpt-4o", is_active=True)
    pinned_id = await _seed_credential(conn, model="gpt-3.5-turbo", is_active=False)
    persona = await _seed_default_persona(conn, identity="x")
    await personas_repo.update(conn, persona.id, llm_credential_id=pinned_id)
    ctx = await resolve_persona_context("web", conn)
    assert ctx.credential.id == pinned_id
    assert ctx.model == "gpt-3.5-turbo"


@pytest.mark.asyncio
async def test_resolve_context_persona_model_overrides_cred_model(conn):
    cred_id = await _seed_credential(conn, model="gpt-4o", is_active=True)
    persona = await _seed_default_persona(conn, identity="x")
    await personas_repo.update(conn, persona.id, llm_credential_id=cred_id, model="gpt-4-turbo")
    ctx = await resolve_persona_context("web", conn)
    assert ctx.model == "gpt-4-turbo"


@pytest.mark.asyncio
async def test_resolve_context_no_credential_raises_503(conn):
    from fastapi import HTTPException
    await _seed_default_persona(conn, identity="x")
    with pytest.raises(HTTPException) as exc_info:
        await resolve_persona_context("web", conn)
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_resolve_context_model_falls_back_to_cred_default_when_persona_model_null(conn):
    cred_id = await _seed_credential(conn, model="gpt-4o", is_active=True)
    persona = await _seed_default_persona(conn, identity="x")
    # persona.model is None by default — should fall back to credential.model
    await personas_repo.update(conn, persona.id, llm_credential_id=cred_id)
    ctx = await resolve_persona_context("web", conn)
    assert ctx.model == "gpt-4o"
