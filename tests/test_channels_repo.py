"""Unit tests for the channel_prompts repository (Plan 29-A)."""
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import CHANNEL_REGISTRY
from hermes.repository import channels as repo
from hermes.repository import personas as personas_repo


@pytest.mark.asyncio
async def test_list_empty_on_fresh_db(conn: AsyncEngine) -> None:
    assert await repo.list_all(conn) == []
    assert await repo.get(conn, "web") is None


@pytest.mark.asyncio
async def test_ensure_seeded_creates_one_row_per_channel(
    conn: AsyncEngine,
) -> None:
    inserted = await repo.ensure_seeded(conn)
    assert sorted(inserted) == sorted(CHANNEL_REGISTRY.keys())

    rows = await repo.list_all(conn)
    assert [r.channel for r in rows] == list(CHANNEL_REGISTRY.keys())
    for row in rows:
        assert row.prompt == CHANNEL_REGISTRY[row.channel]["default_prompt"]
        assert row.default_persona_id is None


@pytest.mark.asyncio
async def test_ensure_seeded_is_idempotent(conn: AsyncEngine) -> None:
    first = await repo.ensure_seeded(conn)
    second = await repo.ensure_seeded(conn)
    assert sorted(first) == sorted(CHANNEL_REGISTRY.keys())
    assert second == []
    assert len(await repo.list_all(conn)) == len(CHANNEL_REGISTRY)


@pytest.mark.asyncio
async def test_update_prompt_only(conn: AsyncEngine) -> None:
    await repo.ensure_seeded(conn)
    updated = await repo.update(conn, "web", prompt="custom web prompt")
    assert updated is not None
    assert updated.prompt == "custom web prompt"
    assert updated.default_persona_id is None


@pytest.mark.asyncio
async def test_update_default_persona_only(conn: AsyncEngine) -> None:
    await repo.ensure_seeded(conn)
    persona = await personas_repo.create(
        conn, name="A", soul="", identity="a", agents="", is_default=True
    )

    updated = await repo.update(conn, "web", default_persona_id=persona.id)
    assert updated is not None
    assert updated.default_persona_id == persona.id
    # Prompt unchanged.
    assert updated.prompt == CHANNEL_REGISTRY["web"]["default_prompt"]


@pytest.mark.asyncio
async def test_update_unknown_channel_returns_none(conn: AsyncEngine) -> None:
    await repo.ensure_seeded(conn)
    assert await repo.update(conn, "discord", prompt="x") is None


@pytest.mark.asyncio
async def test_reset_prompt(conn: AsyncEngine) -> None:
    await repo.ensure_seeded(conn)
    persona = await personas_repo.create(
        conn, name="A", soul="", identity="a", agents="", is_default=True
    )
    await repo.update(
        conn, "web", prompt="custom", default_persona_id=persona.id
    )

    reset = await repo.reset_prompt(conn, "web")
    assert reset is not None
    assert reset.prompt == CHANNEL_REGISTRY["web"]["default_prompt"]
    # default_persona_id unchanged.
    assert reset.default_persona_id == persona.id


@pytest.mark.asyncio
async def test_reset_unknown_channel_returns_none(conn: AsyncEngine) -> None:
    await repo.ensure_seeded(conn)
    assert await repo.reset_prompt(conn, "discord") is None


@pytest.mark.asyncio
async def test_delete_persona_nulls_channel_default(
    conn: AsyncEngine,
) -> None:
    """FK ON DELETE SET NULL on channel_prompts.default_persona_id."""
    await repo.ensure_seeded(conn)
    default_persona = await personas_repo.create(
        conn,
        name="Default",
        soul="",
        identity="d",
        agents="",
        is_default=True,
    )
    other = await personas_repo.create(
        conn,
        name="Other",
        soul="",
        identity="o",
        agents="",
        is_default=False,
    )
    await repo.update(conn, "web", default_persona_id=other.id)

    deleted = await personas_repo.delete(conn, other.id)
    assert deleted is True

    row = await repo.get(conn, "web")
    assert row is not None
    assert row.default_persona_id is None
    # Default persona untouched.
    assert await personas_repo.get(conn, default_persona.id) is not None
