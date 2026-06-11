"""Database bootstrap (Postgres + RLS).

`init_db()` runs Alembic to `head` (using the owner role) and returns an
AsyncEngine that connects as `holzi_app` — the role with NOBYPASSRLS, the
one RLS actually bites. Per-request code uses `tx_for_user(engine)` to open
a transaction with `SET LOCAL app.user_id = $1` applied; the resolved
user_id is read from the `current_user_id` ContextVar populated by the
auth middleware.
"""
import asyncio
import contextlib
from contextvars import ContextVar, Token
from typing import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from hermes.config import settings


_current_user_id: ContextVar[int | None] = ContextVar("_current_user_id", default=None)


def set_current_user_token(user_id: int) -> Token:
    """Bind `user_id` to this task's ContextVar; returns a Token to reset."""
    return _current_user_id.set(user_id)


def reset_current_user(token: Token) -> None:
    """Restore the prior ContextVar value (call in the request's finally)."""
    _current_user_id.reset(token)


def get_current_user() -> int | None:
    return _current_user_id.get()


def _owner_url() -> str:
    return settings.database_url


def _runtime_url() -> str:
    if settings.runtime_database_url:
        return settings.runtime_database_url
    # Derive: same host/port/db, swap role + password.
    parts = urlsplit(settings.database_url)
    if not parts.hostname:
        raise RuntimeError(f"cannot derive runtime URL from {settings.database_url!r}")
    netloc = f"holzi_app:{settings.runtime_role_password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def init_db() -> AsyncEngine:
    """Run Alembic to head as owner, then return the holzi_app engine.

    Alembic command runs synchronously; we hop into a thread to keep the
    event loop responsive.
    """
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _owner_url())
    await asyncio.to_thread(command.upgrade, cfg, "head")
    return create_async_engine(_runtime_url(), pool_pre_ping=True)


async def make_owner_engine() -> AsyncEngine:
    """Separate engine for owner-role paths (lifespan seeding, global sweepers).
    Disposed by the lifespan teardown. Use with `tx_as_owner(...)`.
    """
    return create_async_engine(_owner_url(), pool_pre_ping=True, pool_size=2)


@contextlib.asynccontextmanager
async def tx_for_user(
    engine: AsyncEngine, *, user_id: int | None = None
) -> AsyncIterator[AsyncConnection]:
    """Open a transaction with `SET LOCAL app.user_id = $1`.

    Resolution order: explicit `user_id` arg > ContextVar > raise.
    The middleware populates the ContextVar; repository code that runs
    outside a request (lifespan seeding, the scheduler) must pass `user_id`
    explicitly. A None resolution is a programming error — RLS would silently
    return zero rows, which is the worst possible failure mode.
    """
    uid = user_id if user_id is not None else _current_user_id.get()
    if uid is None:
        raise RuntimeError(
            "tx_for_user requires a resolved user_id "
            "(ContextVar empty and no explicit kwarg)"
        )
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.user_id = :u"), {"u": str(uid)})
        yield conn


@contextlib.asynccontextmanager
async def tx_as_owner(owner_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Escape-hatch for lifespan bootstrap (seed platform admin) and the
    scheduler's GLOBAL queries (e.g. `list_expired`, `agent_tasks list_due`).
    Connects as `holzi_owner` — RLS still applies (FORCE), but the GUC
    default of '0' lets owner queries see zero rows from personal tables
    unless they also `SET LOCAL app.user_id`. The bootstrap inserts users
    while connected as owner, then uses `tx_for_user` for everything after.
    """
    async with owner_engine.begin() as conn:
        yield conn
