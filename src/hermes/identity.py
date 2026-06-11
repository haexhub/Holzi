"""Identity resolution seam (Wave C1, Plan 35 §C1).

The per-request bearer is a SESSION token. SessionResolver maps it to an
Identity via the sessions table, honouring expiry. How a session is *minted*
(email magic-link in C2, DID in haex-vault later) is a separate login
strategy — this resolver is unaffected by it.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.schema import sessions, users


def hash_token(credential: str) -> str:
    """SHA-256 hex of a bearer/session token. High-entropy random tokens, so
    a plain digest keeps live tokens out of a DB dump."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: int
    role: str


class IdentityResolver(Protocol):
    async def resolve(self, credential: str) -> Identity | None: ...


class SessionResolver:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def resolve(self, credential: str) -> Identity | None:
        token_hash = hash_token(credential)
        now = int(time.time())
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(users.c.id, users.c.role)
                    .select_from(
                        sessions.join(users, sessions.c.user_id == users.c.id)
                    )
                    .where(sessions.c.token_hash == token_hash)
                    .where(
                        (sessions.c.expires_at.is_(None))
                        | (sessions.c.expires_at > now)
                    )
                )
            ).first()
        return Identity(user_id=row.id, role=row.role) if row else None
