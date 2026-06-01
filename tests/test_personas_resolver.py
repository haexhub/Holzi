"""Unit tests for the effective-system-prompt resolver (Plan 29-A)."""
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import (
    CHANNEL_REGISTRY,
    DEFAULT_PERSONA_NAME,
    DEFAULT_PERSONA_PROMPT,
    ensure_backfill,
    get_effective_system_prompt,
)
from hermes.repository import channels as channels_repo
from hermes.repository import personas as personas_repo


@pytest.mark.asyncio
async def test_unknown_channel_raises_key_error(conn: AsyncEngine) -> None:
    await ensure_backfill(conn)
    with pytest.raises(KeyError):
        await get_effective_system_prompt("discord", conn)


@pytest.mark.asyncio
async def test_default_composition_after_backfill(conn: AsyncEngine) -> None:
    await ensure_backfill(conn)

    prompt = await get_effective_system_prompt("web", conn)
    expected = (
        f"{DEFAULT_PERSONA_PROMPT}\n\n"
        f"{CHANNEL_REGISTRY['web']['default_prompt']}"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_custom_channel_prompt_composes_with_default_persona(
    conn: AsyncEngine,
) -> None:
    await ensure_backfill(conn)
    await channels_repo.update(conn, "signal", prompt="single-line only")

    prompt = await get_effective_system_prompt("signal", conn)
    assert prompt == f"{DEFAULT_PERSONA_PROMPT}\n\nsingle-line only"


@pytest.mark.asyncio
async def test_channel_specific_persona_wins_over_global_default(
    conn: AsyncEngine,
) -> None:
    await ensure_backfill(conn)
    custom = await personas_repo.create(
        conn,
        name="Strict Reviewer",
        prompt="Be merciless about types.",
        is_default=False,
    )
    await channels_repo.update(conn, "task", default_persona_id=custom.id)

    prompt = await get_effective_system_prompt("task", conn)
    expected = (
        f"Be merciless about types.\n\n"
        f"{CHANNEL_REGISTRY['task']['default_prompt']}"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_deleting_channel_persona_falls_back_to_global_default(
    conn: AsyncEngine,
) -> None:
    await ensure_backfill(conn)
    custom = await personas_repo.create(
        conn, name="Custom", prompt="custom voice", is_default=False
    )
    await channels_repo.update(conn, "telegram", default_persona_id=custom.id)

    deleted = await personas_repo.delete(conn, custom.id)
    assert deleted is True

    prompt = await get_effective_system_prompt("telegram", conn)
    expected = (
        f"{DEFAULT_PERSONA_PROMPT}\n\n"
        f"{CHANNEL_REGISTRY['telegram']['default_prompt']}"
    )
    assert prompt == expected


@pytest.mark.asyncio
async def test_default_persona_is_named_hermes_after_backfill(
    conn: AsyncEngine,
) -> None:
    await ensure_backfill(conn)
    default = await personas_repo.get_default(conn)
    assert default is not None
    assert default.name == DEFAULT_PERSONA_NAME
    assert default.prompt == DEFAULT_PERSONA_PROMPT


@pytest.mark.asyncio
async def test_backfill_is_idempotent(conn: AsyncEngine) -> None:
    await ensure_backfill(conn)
    personas_before = await personas_repo.list_all(conn)
    channels_before = await channels_repo.list_all(conn)

    await ensure_backfill(conn)
    personas_after = await personas_repo.list_all(conn)
    channels_after = await channels_repo.list_all(conn)

    assert len(personas_before) == len(personas_after) == 1
    assert len(channels_before) == len(channels_after) == len(CHANNEL_REGISTRY)
