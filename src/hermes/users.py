"""Platform admin bootstrap (§1, refined by §2).

Single source of truth for the env-seeded `platform_admin`: a users row
(email + role='platform_admin'), idempotent, plus a never-expiring session
mapping HERMES_PLATFORM_ADMIN_TOKEN → that user. Rotating the env token
drops the previous bootstrap session so the old token stops working.
"""
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.config import settings
from hermes.identity import hash_token

BOOTSTRAP_LABEL = "bootstrap platform_admin"


async def ensure_platform_admin_seeded(owner_engine: AsyncEngine) -> int:
    """Idempotent. Returns the platform_admin's user id. Run from lifespan
    against the OWNER engine — bypasses RLS by design, since at boot there
    is no resolved user yet.
    """
    now = int(time.time())
    token_hash = hash_token(settings.platform_admin_token)
    email = settings.platform_admin_email

    async with owner_engine.begin() as conn:
        row = (await conn.execute(
            text(
                "INSERT INTO users(email, role, bootstrap_completed, created_at) "
                "VALUES (:e, 'platform_admin', false, :now) "
                "ON CONFLICT (email) DO UPDATE SET role = 'platform_admin' "
                "RETURNING id"
            ),
            {"e": email, "now": now},
        )).first()
        if row is None:
            raise RuntimeError("ensure_platform_admin_seeded: users upsert returned no row")
        user_id = row.id

        # Drop any stale bootstrap session whose token_hash no longer matches
        # — token rotation must invalidate the previous token.
        await conn.execute(
            text("DELETE FROM sessions WHERE label = :l AND token_hash != :h"),
            {"l": BOOTSTRAP_LABEL, "h": token_hash},
        )

        await conn.execute(
            text(
                "INSERT INTO sessions(user_id, token_hash, label, created_at, expires_at) "
                "VALUES (:uid, :h, :l, :now, NULL) "
                "ON CONFLICT (token_hash) DO NOTHING"
            ),
            {"uid": user_id, "h": token_hash, "l": BOOTSTRAP_LABEL, "now": now},
        )

    return user_id


async def is_bootstrap_completed(engine: AsyncEngine, user_id: int) -> bool:
    """Returns False if the row is missing entirely (defensive).

    `users` is not under RLS (it's the lookup table the policies depend
    on), so a direct SELECT works against either engine — lifespan calls
    with the owner engine, request flow calls with the runtime engine.
    """
    async with engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT bootstrap_completed FROM users WHERE id = :uid"),
            {"uid": user_id},
        )).first()
    return bool(row and row.bootstrap_completed)
