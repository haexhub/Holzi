import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.starter_skills import ensure_starter_skills_seeded, STARTER_SKILLS


@pytest.mark.asyncio
async def test_all_starter_skills_seeded(conn: AsyncEngine) -> None:
    """All 8 starter skills are present after ensure_starter_skills_seeded."""
    await ensure_starter_skills_seeded(conn)
    async with conn.connect() as c:
        rows = (await c.execute(text("SELECT slug FROM skills ORDER BY slug"))).all()
    slugs = {r.slug for r in rows}
    for skill in STARTER_SKILLS:
        assert skill["slug"] in slugs, f"Missing skill: {skill['slug']}"


@pytest.mark.asyncio
async def test_starter_skills_all_enabled(conn: AsyncEngine) -> None:
    """All seeded skills default to enabled=1."""
    await ensure_starter_skills_seeded(conn)
    async with conn.connect() as c:
        rows = (
            await c.execute(
                text("SELECT slug, enabled FROM skills")
            )
        ).all()
    for row in rows:
        assert row.enabled == 1, f"{row.slug} should be enabled"


@pytest.mark.asyncio
async def test_starter_skills_seeded_idempotent(conn: AsyncEngine) -> None:
    """Running ensure_starter_skills_seeded twice creates no duplicate rows."""
    await ensure_starter_skills_seeded(conn)
    await ensure_starter_skills_seeded(conn)
    async with conn.connect() as c:
        count = (
            await c.execute(text("SELECT COUNT(*) FROM skills"))
        ).scalar()
    assert count == len(STARTER_SKILLS)


@pytest.mark.asyncio
async def test_starter_skills_preserves_user_edit(conn: AsyncEngine) -> None:
    """A manually edited body is NOT overwritten on second boot."""
    await ensure_starter_skills_seeded(conn)
    async with conn.begin() as c:
        await c.execute(
            text("UPDATE skills SET body_markdown = 'custom body' WHERE slug = 'code-review'")
        )
    await ensure_starter_skills_seeded(conn)
    async with conn.connect() as c:
        row = (
            await c.execute(
                text("SELECT body_markdown FROM skills WHERE slug = 'code-review'")
            )
        ).first()
    assert row is not None and row.body_markdown == "custom body"


@pytest.mark.asyncio
async def test_starter_skills_count(conn: AsyncEngine) -> None:
    """STARTER_SKILLS contains exactly 8 entries."""
    assert len(STARTER_SKILLS) == 8
