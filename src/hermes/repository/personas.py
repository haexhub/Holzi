"""Persistence layer for `personas` (Plan 29-A).

Personas are the *who* of the agent — identity + style. Each row carries
a name (UNIQUE) and an opaque prompt string. At most one row has
`is_default = 1`, enforced by triggers in `schema.sql`: inserting or
updating any row with `is_default = 1` demotes every other row.

Deletion of the default persona is refused at the repo layer (returns
False) so callers can surface a 422 — without a default, the resolver
has no fallback for channels with `default_persona_id` NULL.
"""
import time

from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Persona
from hermes.schema import personas as t_personas


def _row_to_persona(row) -> Persona:
    return Persona(
        id=row.id,
        name=row.name,
        prompt=row.prompt,
        is_default=bool(row.is_default),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_all(engine: AsyncEngine) -> list[Persona]:
    """Default-first, then alphabetical — drives the UI list order."""
    stmt = select(t_personas).order_by(
        desc(t_personas.c.is_default),
        asc(t_personas.c.name),
    )
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_persona(r) for r in rows]


async def get(engine: AsyncEngine, persona_id: int) -> Persona | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_personas).where(t_personas.c.id == persona_id)
        )
        row = result.first()
    return _row_to_persona(row) if row is not None else None


async def get_by_name(engine: AsyncEngine, name: str) -> Persona | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_personas).where(t_personas.c.name == name)
        )
        row = result.first()
    return _row_to_persona(row) if row is not None else None


async def get_default(engine: AsyncEngine) -> Persona | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_personas).where(t_personas.c.is_default == 1)
        )
        row = result.first()
    return _row_to_persona(row) if row is not None else None


async def create(
    engine: AsyncEngine,
    *,
    name: str,
    prompt: str,
    is_default: bool = False,
    ts: int | None = None,
) -> Persona:
    """Insert a row. UNIQUE(name) means a duplicate raises IntegrityError —
    route layer translates to 409. Triggers demote any prior default if
    `is_default=True`."""
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        result = await conn.execute(
            t_personas.insert()
            .values(
                name=name,
                prompt=prompt,
                is_default=1 if is_default else 0,
                created_at=now,
                updated_at=now,
            )
            .returning(
                t_personas.c.id,
                t_personas.c.name,
                t_personas.c.prompt,
                t_personas.c.is_default,
                t_personas.c.created_at,
                t_personas.c.updated_at,
            )
        )
        row = result.first()
    if row is None:
        raise RuntimeError("personas insert ... RETURNING returned no row")
    return _row_to_persona(row)


async def update(
    engine: AsyncEngine,
    persona_id: int,
    *,
    name: str | None = None,
    prompt: str | None = None,
    is_default: bool | None = None,
    ts: int | None = None,
) -> Persona | None:
    """Patch a row. None means "leave this field alone". Demotion of
    other defaults happens via the schema.sql trigger."""
    existing = await get(engine, persona_id)
    if existing is None:
        return None

    new_name = name if name is not None else existing.name
    new_prompt = prompt if prompt is not None else existing.prompt
    new_is_default = (
        existing.is_default if is_default is None else is_default
    )

    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            t_personas.update()
            .where(t_personas.c.id == persona_id)
            .values(
                name=new_name,
                prompt=new_prompt,
                is_default=1 if new_is_default else 0,
                updated_at=now,
            )
        )
    return await get(engine, persona_id)


async def delete(engine: AsyncEngine, persona_id: int) -> bool:
    """Delete a row. Refuses to delete the default persona (returns False)
    so the resolver always has a fallback. FK ON DELETE SET NULL on
    `channel_prompts.default_persona_id` cleans up channel assignments.
    """
    existing = await get(engine, persona_id)
    if existing is None:
        return False
    if existing.is_default:
        return False
    async with engine.begin() as conn:
        result = await conn.execute(
            t_personas.delete().where(t_personas.c.id == persona_id)
        )
    return (result.rowcount or 0) > 0
