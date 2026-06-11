"""Persistence layer for `personas` (Plan 29-A → fragments per Plan 36).

Personas are the *who* of the agent — identity + style. Each row carries
a name (UNIQUE) and three opaque prompt fragments (`soul`, `identity`,
`agents`) that the resolver composes at runtime. At most one row has
`is_default = 1`, enforced by repo-layer logic: inserting or
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

from hermes.db import tx_for_user
from hermes.repository import persona_history as history_repo
from hermes.repository.models import Persona
from hermes.schema import personas as t_personas

_UNSET = object()


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
        llm_credential_id=row.llm_credential_id,
        model=row.model,
        user_id=row.user_id,
    )


async def list_all(engine: AsyncEngine, *, user_id: int) -> list[Persona]:
    """Default-first, then alphabetical — drives the UI list order.
    Scoped to the caller — a user only sees their own personas."""
    stmt = (
        select(t_personas)
        .where(t_personas.c.user_id == user_id)
        .order_by(
            desc(t_personas.c.is_default),
            asc(t_personas.c.name),
        )
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_persona(r) for r in rows]


async def get(
    engine: AsyncEngine, persona_id: int, *, user_id: int
) -> Persona | None:
    """Fetch a persona the caller owns. Another user's persona returns None."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_personas).where(
                t_personas.c.id == persona_id,
                t_personas.c.user_id == user_id,
            )
        )
        row = result.first()
    return _row_to_persona(row) if row is not None else None


async def get_by_name(
    engine: AsyncEngine, name: str, *, user_id: int
) -> Persona | None:
    """Fetch the caller's persona by name. Scoped — a name owned by another
    user returns None (names are unique per user)."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_personas).where(
                t_personas.c.name == name,
                t_personas.c.user_id == user_id,
            )
        )
        row = result.first()
    return _row_to_persona(row) if row is not None else None


async def get_default(engine: AsyncEngine, *, user_id: int) -> Persona | None:
    """Return THIS user's default persona (per-user single-default invariant)."""
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_personas).where(
                t_personas.c.is_default.is_(True),
                t_personas.c.user_id == user_id,
            )
        )
        row = result.first()
    return _row_to_persona(row) if row is not None else None


async def create(
    engine: AsyncEngine,
    *,
    user_id: int,
    name: str,
    soul: str,
    identity: str,
    agents: str,
    is_default: bool = False,
    llm_credential_id: int | None = None,
    model: str | None = None,
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
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            t_personas.insert()
            .values(
                user_id=user_id,
                name=name,
                soul=soul,
                identity=identity,
                agents=agents,
                is_default=1 if is_default else 0,
                llm_credential_id=llm_credential_id,
                model=model,
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
                t_personas.c.llm_credential_id,
                t_personas.c.model,
                t_personas.c.user_id,
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
    user_id: int,
    name: str | None = None,
    soul: str | None = None,
    identity: str | None = None,
    agents: str | None = None,
    is_default: bool | None = None,
    llm_credential_id: int | None | object = _UNSET,
    model: str | None | object = _UNSET,
    ts: int | None = None,
    history_author: str = "user",
) -> Persona | None:
    """Patch a row. None means "leave this field alone". Demotion of
    other defaults happens here in the repo layer (scoped per user).

    Scoped to the owner: another user's persona returns None (no-op).

    On a successful update a `persona_history` row capturing the
    post-update state is appended inside the same transaction.
    `history_author` labels who issued this write (``"user"`` for
    UI edits, ``"bootstrap"`` for the bootstrap skill, etc.).
    """
    existing = await get(engine, persona_id, user_id=user_id)
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
    updates: dict = {
        t_personas.c.name: new_name,
        t_personas.c.soul: new_soul,
        t_personas.c.identity: new_identity,
        t_personas.c.agents: new_agents,
        t_personas.c.is_default: 1 if new_is_default else 0,
        t_personas.c.updated_at: now,
    }
    if llm_credential_id is not _UNSET:
        updates[t_personas.c.llm_credential_id] = llm_credential_id
    if model is not _UNSET:
        updates[t_personas.c.model] = model
    async with tx_for_user(engine, user_id=user_id) as conn:
        await conn.execute(
            t_personas.update()
            .where(
                t_personas.c.id == persona_id,
                t_personas.c.user_id == user_id,
            )
            .values(updates)
        )
        # Read the post-update row on the same connection so the
        # snapshot reflects exactly what the UPDATE produced (including
        # any trigger-driven demotion of `is_default` on this row).
        result = await conn.execute(
            select(t_personas).where(
                t_personas.c.id == persona_id,
                t_personas.c.user_id == user_id,
            )
        )
        row = result.first()
        if row is None:
            return None
        persona = _row_to_persona(row)
        await history_repo.write_snapshot(
            engine, persona, author=history_author, ts=now, conn=conn
        )
    return persona


async def delete(engine: AsyncEngine, persona_id: int, *, user_id: int) -> bool:
    """Delete a row the caller owns. Refuses to delete the default persona
    (returns False) so the resolver always has a fallback. A persona
    belonging to another user is a no-op (returns False) — the `user_id`
    filter is part of the DELETE WHERE. FK ON DELETE SET NULL on
    `channel_prompts.default_persona_id` cleans up channel assignments,
    and ON DELETE CASCADE on `persona_history.persona_id` wipes the
    audit rows for the gone persona.
    """
    # Atomic guard: include the is_default=0 condition in the DELETE
    # itself rather than checking first via `get()` and then DELETEing
    # — that pattern has a TOCTOU race where a concurrent UPDATE can
    # promote the row to default between read and delete. The single
    # DELETE … WHERE is_default = 0 is race-free; rowcount=0 means
    # either the row didn't exist or it was the default.
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            t_personas.delete().where(
                (t_personas.c.id == persona_id)
                & (t_personas.c.is_default.is_(False))
                & (t_personas.c.user_id == user_id)
            )
        )
    return (result.rowcount or 0) > 0
