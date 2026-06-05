"""Tests for ensure_bootstrap_skill_seeded (Plan 37 Task 6)."""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.personas import ensure_bootstrap_skill_seeded
from hermes.repository import skills as skills_repo


@pytest.mark.asyncio
async def test_bootstrap_skill_seeded_on_first_boot(conn: AsyncEngine) -> None:
    """Fresh DB → ensure_bootstrap_skill_seeded → skill exists with enabled=1."""
    await ensure_bootstrap_skill_seeded(conn)

    skill = await skills_repo.get_by_slug(conn, "bootstrap-first-chat")
    assert skill is not None
    assert skill.slug == "bootstrap-first-chat"
    assert skill.enabled is True
    assert skill.body_markdown  # non-empty body


@pytest.mark.asyncio
async def test_bootstrap_skill_seed_idempotent_no_overwrite(
    conn: AsyncEngine,
) -> None:
    """Second boot does not overwrite a manually-edited body (INSERT OR IGNORE)."""
    await ensure_bootstrap_skill_seeded(conn)

    # Simulate user editing the body
    async with conn.begin() as txn:
        await txn.execute(
            text(
                "UPDATE skills SET body_markdown = 'CUSTOM BODY' "
                "WHERE slug = 'bootstrap-first-chat'"
            )
        )

    # Second boot
    await ensure_bootstrap_skill_seeded(conn)

    skill = await skills_repo.get_by_slug(conn, "bootstrap-first-chat")
    assert skill is not None
    assert skill.body_markdown == "CUSTOM BODY"
