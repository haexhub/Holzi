"""Persistence layer for `skills` (Plan 33 → Plan 37).

Skills are reusable prompt building blocks (Markdown body + structured
frontmatter-style fields). The catalog index in the resolver exposes
slug + description + when_to_use to the agent; the full body is loaded
on demand via the `skill_load` tool.

`persona_skills` (Plan 33 per-persona activation) was dropped in Plan 37 —
skills are now universally available via the catalog index.
"""
import time

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Skill
from hermes.schema import skills as t_skills


def _row_to_skill(row) -> Skill:
    return Skill(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        when_to_use=row.when_to_use or "",
        body_markdown=row.body_markdown,
        enabled=bool(row.enabled),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_all(engine: AsyncEngine) -> list[Skill]:
    """Alphabetical by slug — stable order for the UI list."""
    stmt = select(t_skills).order_by(asc(t_skills.c.slug))
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_skill(r) for r in rows]


async def get(engine: AsyncEngine, skill_id: int) -> Skill | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_skills).where(t_skills.c.id == skill_id)
        )
        row = result.first()
    return _row_to_skill(row) if row is not None else None


async def get_by_slug(engine: AsyncEngine, slug: str) -> Skill | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_skills).where(t_skills.c.slug == slug)
        )
        row = result.first()
    return _row_to_skill(row) if row is not None else None


async def list_enabled(engine: AsyncEngine) -> list[Skill]:
    """Alphabetical by slug — for the resolver catalog index."""
    stmt = (
        select(t_skills)
        .where(t_skills.c.enabled.is_(True))
        .order_by(asc(t_skills.c.slug))
    )
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_skill(r) for r in rows]


async def create(
    engine: AsyncEngine,
    *,
    slug: str,
    name: str,
    description: str,
    when_to_use: str = "",
    body_markdown: str,
    enabled: bool = True,
    ts: int | None = None,
) -> Skill:
    """Insert a row. UNIQUE(slug) means a duplicate raises IntegrityError —
    the route layer maps that to a 409."""
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        result = await conn.execute(
            t_skills.insert()
            .values(
                slug=slug,
                name=name,
                description=description,
                when_to_use=when_to_use,
                body_markdown=body_markdown,
                enabled=1 if enabled else 0,
                created_at=now,
                updated_at=now,
            )
            .returning(
                t_skills.c.id,
                t_skills.c.slug,
                t_skills.c.name,
                t_skills.c.description,
                t_skills.c.when_to_use,
                t_skills.c.body_markdown,
                t_skills.c.enabled,
                t_skills.c.created_at,
                t_skills.c.updated_at,
            )
        )
        row = result.first()
    if row is None:
        raise RuntimeError("skills insert ... RETURNING returned no row")
    return _row_to_skill(row)


async def update(
    engine: AsyncEngine,
    skill_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    when_to_use: str | None = None,
    body_markdown: str | None = None,
    enabled: bool | None = None,
    ts: int | None = None,
) -> Skill | None:
    """Patch a row. None means "leave this field alone". Slug is
    immutable — to rename a skill, delete and recreate."""
    existing = await get(engine, skill_id)
    if existing is None:
        return None

    new_name = name if name is not None else existing.name
    new_description = (
        description if description is not None else existing.description
    )
    new_when = (
        when_to_use if when_to_use is not None else existing.when_to_use
    )
    new_body = (
        body_markdown if body_markdown is not None else existing.body_markdown
    )
    new_enabled = 1 if (enabled if enabled is not None else existing.enabled) else 0

    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            t_skills.update()
            .where(t_skills.c.id == skill_id)
            .values(
                name=new_name,
                description=new_description,
                when_to_use=new_when,
                body_markdown=new_body,
                enabled=new_enabled,
                updated_at=now,
            )
        )
    return await get(engine, skill_id)


async def delete(engine: AsyncEngine, skill_id: int) -> bool:
    """Delete a row."""
    async with engine.begin() as conn:
        result = await conn.execute(
            t_skills.delete().where(t_skills.c.id == skill_id)
        )
    return (result.rowcount or 0) > 0
