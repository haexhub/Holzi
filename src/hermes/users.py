"""Single-user state (Plan 37).

Wave-C-vorbereitend: minimal `users` table with bootstrap_completed flag.
The `users` table now carries email/role/parent_user_id (Wave C1); user 1 is
the admin and gets a never-expiring bootstrap session mapping the operator's
`HERMES_AUTH_TOKEN` to the admin identity via SessionResolver.
"""
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.config import settings
from hermes.identity import hash_token


async def ensure_users_seeded(engine: AsyncEngine) -> None:
    """First-boot seed: insert the admin user row (id=1, role='admin',
    bootstrap_completed=0) and a never-expiring bootstrap session mapping
    hash_token(settings.auth_token) → user 1. Idempotent — INSERT OR IGNORE
    matches the users PK and the sessions UNIQUE(token_hash).
    """
    now = int(time.time())
    token_hash = hash_token(settings.auth_token)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users(id, role, bootstrap_completed, created_at) "
                "VALUES (1, 'admin', 0, :now)"
            ),
            {"now": now},
        )
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO sessions"
                "(user_id, token_hash, label, created_at, expires_at) "
                "VALUES (1, :h, 'bootstrap admin', :now, NULL)"
            ),
            {"h": token_hash, "now": now},
        )


async def is_bootstrap_completed(engine: AsyncEngine) -> bool:
    """Returns False if the row is missing entirely (defensive)."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT bootstrap_completed FROM users WHERE id = 1")
            )
        ).first()
    return bool(row and row.bootstrap_completed)
