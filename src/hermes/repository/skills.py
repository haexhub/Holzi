"""Persistence layer for `skills` + `persona_skills` (Plan 33).

Skills are reusable prompt building blocks (Markdown body + structured
frontmatter-style fields). `persona_skills` is the m:n link that says
"persona X has skill Y enabled at ordering Z" — the resolver in
`hermes.personas` joins the active rows in order and mixes the bodies
into the effective system prompt.

`set_persona_skills` runs DELETE-then-INSERTs in a single transaction so
a malformed item (e.g. unknown `skill_id`) rolls back the entire
replacement and the persona keeps its prior list — the route layer maps
the IntegrityError to a 422 / 409 response.
"""
import time

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Skill
from hermes.schema import persona_skills as t_persona_skills
from hermes.schema import skills as t_skills


def _row_to_skill(row) -> Skill:
    return Skill(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        when_to_use=row.when_to_use,
        body_markdown=row.body_markdown,
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


async def create(
    engine: AsyncEngine,
    *,
    slug: str,
    name: str,
    description: str,
    when_to_use: str | None,
    body_markdown: str,
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
                updated_at=now,
            )
        )
    return await get(engine, skill_id)


async def delete(engine: AsyncEngine, skill_id: int) -> bool:
    """Delete a row. CASCADE drops every `persona_skills` link to it."""
    async with engine.begin() as conn:
        result = await conn.execute(
            t_skills.delete().where(t_skills.c.id == skill_id)
        )
    return (result.rowcount or 0) > 0


async def list_for_persona(
    engine: AsyncEngine, persona_id: int
) -> list[tuple[Skill, int, bool]]:
    """Return (skill, ordering, enabled) tuples for a persona, sorted by
    `ordering` ascending. Used by the resolver to compose the prompt and
    by the UI to render the persona's active skills."""
    stmt = (
        select(
            t_skills,
            t_persona_skills.c.ordering,
            t_persona_skills.c.enabled,
        )
        .join(t_persona_skills, t_persona_skills.c.skill_id == t_skills.c.id)
        .where(t_persona_skills.c.persona_id == persona_id)
        .order_by(asc(t_persona_skills.c.ordering))
    )
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [
        (
            Skill(
                id=row.id,
                slug=row.slug,
                name=row.name,
                description=row.description,
                when_to_use=row.when_to_use,
                body_markdown=row.body_markdown,
                created_at=row.created_at,
                updated_at=row.updated_at,
            ),
            row.ordering,
            bool(row.enabled),
        )
        for row in rows
    ]


async def set_persona_skills(
    engine: AsyncEngine,
    persona_id: int,
    items: list[dict],
) -> None:
    """Replace the persona's skill list atomically.

    Steps inside a single transaction:
    1. DELETE every existing `persona_skills` row for this persona.
    2. INSERT each item from `items`.

    Items must be dicts with `skill_id` (int), `ordering` (int), and
    `enabled` (bool). Bad data — unknown `skill_id`, duplicate
    `(persona_id, skill_id)` — raises IntegrityError and the whole tx
    rolls back; the persona's prior list survives.
    """
    async with engine.begin() as conn:
        await conn.execute(
            t_persona_skills.delete().where(
                t_persona_skills.c.persona_id == persona_id
            )
        )
        for item in items:
            await conn.execute(
                t_persona_skills.insert().values(
                    persona_id=persona_id,
                    skill_id=int(item["skill_id"]),
                    ordering=int(item["ordering"]),
                    enabled=1 if item["enabled"] else 0,
                )
            )
