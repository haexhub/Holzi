"""Single-user state (Plan 37).

Wave-C-vorbereitend: minimal `users` table with bootstrap_completed flag.
Wave C will extend with email/password_hash/role/parent_user_id via
ALTER TABLE ADD COLUMN.
"""
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def ensure_users_seeded(engine: AsyncEngine) -> None:
    """First-boot seed: insert the single-user row (id=1,
    bootstrap_completed=0). Idempotent — INSERT OR IGNORE matches on PK.
    """
    now = int(time.time())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users(id, bootstrap_completed, created_at) "
                "VALUES (1, 0, :now)"
            ),
            {"now": now},
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
