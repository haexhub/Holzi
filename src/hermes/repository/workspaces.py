"""Persistence layer for `workspaces` (Plan 25).

The slug (`id`) is the only user-controlled value that ends up touching
a path — `${sandbox_volume_root}/${id}` — so it's validated tightly: a
short kebab-case string, lowercase ASCII + digits + dashes, no leading
or trailing dash. Anything else raises `ValueError` and is mapped to a
caller-visible 400 at the route layer.

Archive is a soft-delete: `archived_at` non-NULL hides the row from
`list_active` but the on-disk directory stays. Hard-delete (rmtree) is
a deliberate Plan-25 non-goal.
"""
import re
import time

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import Workspace
from hermes.schema import workspaces as t_workspaces

# kebab-case slug: 2..64 chars, starts with [a-z0-9], may contain inner
# dashes, never trailing dash. Mirrors the Plan-25 plan note verbatim.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")


def validate_slug(slug: str) -> None:
    """Raise `ValueError` if `slug` is not a valid workspace id."""
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            "workspace id must be kebab-case ASCII (a-z, 0-9, -), 2..64 "
            "chars, no leading/trailing dash"
        )


def _row_to_workspace(row) -> Workspace:
    return Workspace(
        id=row.id,
        display_name=row.display_name,
        created_at=row.created_at,
        archived_at=row.archived_at,
    )


async def get(engine: AsyncEngine, workspace_id: str) -> Workspace | None:
    """Return any workspace by id, archived rows included.

    The route layer filters as needed — this layer is intentionally
    permissive so callers (e.g. legacy id lookups for tombstoned rows)
    can still see the row exists."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_workspaces).where(t_workspaces.c.id == workspace_id)
        )
        row = result.first()
    return _row_to_workspace(row) if row is not None else None


async def list_active(engine: AsyncEngine) -> list[Workspace]:
    """Return all non-archived workspaces, ordered by display name."""
    stmt = (
        select(t_workspaces)
        .where(t_workspaces.c.archived_at.is_(None))
        .order_by(asc(t_workspaces.c.display_name))
    )
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_workspace(r) for r in rows]


async def create(
    engine: AsyncEngine,
    *,
    workspace_id: str,
    display_name: str,
    ts: int | None = None,
) -> Workspace:
    """Insert a new workspace row.

    Raises `ValueError` for invalid slug. Raises an integrity error wrapped
    as `ValueError` ("workspace already exists") for slug collisions so the
    route layer can surface a clean 409.
    """
    validate_slug(workspace_id)
    name = display_name.strip()
    if not name:
        raise ValueError("display_name must not be empty")
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        existing = await conn.execute(
            select(t_workspaces.c.id).where(t_workspaces.c.id == workspace_id)
        )
        if existing.first() is not None:
            raise ValueError("workspace already exists")
        await conn.execute(
            t_workspaces.insert().values(
                id=workspace_id,
                display_name=name,
                created_at=now,
                archived_at=None,
            )
        )
    return Workspace(
        id=workspace_id,
        display_name=name,
        created_at=now,
        archived_at=None,
    )


async def rename(
    engine: AsyncEngine,
    workspace_id: str,
    *,
    display_name: str,
) -> Workspace | None:
    """Update the display name. The slug never changes — it's part of the
    on-disk path. Returns None if no row matches."""
    name = display_name.strip()
    if not name:
        raise ValueError("display_name must not be empty")
    async with engine.begin() as conn:
        result = await conn.execute(
            t_workspaces.update()
            .where(t_workspaces.c.id == workspace_id)
            .values(display_name=name)
        )
        if (result.rowcount or 0) == 0:
            return None
    return await get(engine, workspace_id)


async def archive(
    engine: AsyncEngine,
    workspace_id: str,
    *,
    ts: int | None = None,
) -> Workspace | None:
    """Soft-delete a workspace by setting `archived_at`. Idempotent: an
    already-archived row keeps its earlier archived_at."""
    existing = await get(engine, workspace_id)
    if existing is None:
        return None
    if existing.archived_at is not None:
        return existing
    now = ts if ts is not None else int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            t_workspaces.update()
            .where(t_workspaces.c.id == workspace_id)
            .values(archived_at=now)
        )
    return await get(engine, workspace_id)


async def backfill_from_env(
    engine: AsyncEngine,
    *,
    slugs: list[str],
    ts: int | None = None,
) -> list[str]:
    """Idempotent startup backfill: every slug in `slugs` not already in
    the table is inserted with `display_name = id`. Invalid slugs from
    env are skipped (logged at the caller). Returns the list of slugs
    actually inserted (so the caller can log a one-line summary)."""
    if not slugs:
        return []
    now = ts if ts is not None else int(time.time())
    inserted: list[str] = []
    async with engine.begin() as conn:
        existing_rows = await conn.execute(select(t_workspaces.c.id))
        existing_ids = {r.id for r in existing_rows}
        for slug in slugs:
            if slug in existing_ids:
                continue
            try:
                validate_slug(slug)
            except ValueError:
                continue
            await conn.execute(
                t_workspaces.insert().values(
                    id=slug,
                    display_name=slug,
                    created_at=now,
                    archived_at=None,
                )
            )
            inserted.append(slug)
    return inserted
