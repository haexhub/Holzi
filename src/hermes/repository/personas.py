"""Persistence layer for `personas` (Plan 29-A → fragments per Plan 36).

Personas are the *who* of the agent — identity + style. Each row carries
a name (UNIQUE) and three opaque prompt fragments (`soul`, `identity`,
`agents`) that the resolver composes at runtime. At most one row has
`is_default = 1`, enforced by triggers in `schema.sql`: inserting or
updating any row with `is_default = 1` demotes every other row.

Every successful `create`/`update` also appends a row to
`persona_history` inside the same transaction so the audit log can't
drift from the live table.

Deletion of the default persona is refused at the repo layer (returns
False) so callers can surface a 422 — without a default, the resolver
has no fallback for channels with `default_persona_id` NULL.
"""
import time

from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import persona_history as history_repo
from hermes.repository.models import Persona
from hermes.schema import personas as t_personas


def _row_to_persona(row) -> Persona:
    return Persona(
        id=row.id,
        name=row.name,
        soul=row.soul,
        identity=row.identity,
        agents=row.agents,
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
    soul: str,
    identity: str,
    agents: str,
    is_default: bool = False,
    ts: int | None = None,
    history_author: str = "user",
) -> Persona:
    """Insert a row. UNIQUE(name) means a duplicate raises IntegrityError —
    route layer translates to 409. Triggers demote any prior default if
    `is_default=True`.

    A `persona_history` row capturing the new state is appended inside
    the same transaction (atomic with the INSERT). `history_author`
    defaults to ``"user"`` — the lifespan backfill overrides this to
    ``"system"`` so the seed snapshot is distinguishable from human
    edits in the audit trail.
    """
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        result = await conn.execute(
            t_personas.insert()
            .values(
                name=name,
                soul=soul,
                identity=identity,
                agents=agents,
                is_default=1 if is_default else 0,
                created_at=now,
                updated_at=now,
            )
            .returning(
                t_personas.c.id,
                t_personas.c.name,
                t_personas.c.soul,
                t_personas.c.identity,
                t_personas.c.agents,
                t_personas.c.is_default,
                t_personas.c.created_at,
                t_personas.c.updated_at,
            )
        )
        row = result.first()
        if row is None:
            raise RuntimeError(
                "personas insert ... RETURNING returned no row"
            )
        persona = _row_to_persona(row)
        await history_repo.write_snapshot(
            engine, persona, author=history_author, ts=now, conn=conn
        )
    return persona


async def update(
    engine: AsyncEngine,
    persona_id: int,
    *,
    name: str | None = None,
    soul: str | None = None,
    identity: str | None = None,
    agents: str | None = None,
    is_default: bool | None = None,
    ts: int | None = None,
) -> Persona | None:
    """Patch a row. None means "leave this field alone". Demotion of
    other defaults happens via the schema.sql trigger.

    On a successful update a `persona_history` row capturing the
    post-update state is appended inside the same transaction.
    """
    existing = await get(engine, persona_id)
    if existing is None:
        return None

    new_name = name if name is not None else existing.name
    new_soul = soul if soul is not None else existing.soul
    new_identity = identity if identity is not None else existing.identity
    new_agents = agents if agents is not None else existing.agents
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
                soul=new_soul,
                identity=new_identity,
                agents=new_agents,
                is_default=1 if new_is_default else 0,
                updated_at=now,
            )
        )
        # Read the post-update row on the same connection so the
        # snapshot reflects exactly what the UPDATE produced (including
        # any trigger-driven demotion of `is_default` on this row).
        result = await conn.execute(
            select(t_personas).where(t_personas.c.id == persona_id)
        )
        row = result.first()
        if row is None:
            return None
        persona = _row_to_persona(row)
        await history_repo.write_snapshot(engine, persona, ts=now, conn=conn)
    return persona


async def delete(engine: AsyncEngine, persona_id: int) -> bool:
    """Delete a row. Refuses to delete the default persona (returns False)
    so the resolver always has a fallback. FK ON DELETE SET NULL on
    `channel_prompts.default_persona_id` cleans up channel assignments,
    and ON DELETE CASCADE on `persona_history.persona_id` wipes the
    audit rows for the gone persona.
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
