"""CRUD for the `llm_credentials` table.

Ciphertext flows through here as opaque hex strings — encryption /
decryption is the caller's job (route handlers encrypt on the way in,
the agent loop and the resolver plugin decrypt on the way out).
"""
import time

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import EncryptedBlob
from hermes.db import tx_for_user
from hermes.repository.models import LlmCredential
from hermes.schema import llm_credentials as t


def _row_to_credential(row) -> LlmCredential:
    return LlmCredential(
        id=row.id,
        provider=row.provider,
        mode=row.mode,
        display_name=row.display_name,
        base_url=row.base_url,
        model=row.model,
        is_active=bool(row.is_active),
        api_key_iv=row.api_key_iv,
        api_key_tag=row.api_key_tag,
        api_key_data=row.api_key_data,
        oauth_status=row.oauth_status,
        oauth_authorized_at=row.oauth_authorized_at,
        oauth_iv=row.oauth_iv,
        oauth_tag=row.oauth_tag,
        oauth_data=row.oauth_data,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def create_api_key(
    engine: AsyncEngine,
    *,
    user_id: int,
    provider: str,
    display_name: str,
    base_url: str | None,
    ciphertext: EncryptedBlob,
    ts: int | None = None,
) -> LlmCredential:
    now = ts if ts is not None else int(time.time())
    stmt = (
        t.insert()
        .values(
            provider=provider,
            mode="api_key",
            display_name=display_name,
            base_url=base_url,
            is_active=0,
            api_key_iv=ciphertext.iv,
            api_key_tag=ciphertext.tag,
            api_key_data=ciphertext.data,
            created_at=now,
            updated_at=now,
            user_id=user_id,
        )
        .returning(*t.c)
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        row = result.first()
    if row is None:
        raise RuntimeError("insert into llm_credentials ... RETURNING returned no row")
    return _row_to_credential(row)


async def create_oauth_pending(
    engine: AsyncEngine,
    *,
    user_id: int,
    display_name: str,
    ts: int | None = None,
) -> LlmCredential:
    """Insert a placeholder row for an in-flight Claude OAuth flow.

    Provider is always 'anthropic' for oauth_claude — the only OAuth path
    Hermes supports today. `oauth_status='pending'` until the code-submit
    step swaps in the real ciphertext via `update_oauth_authorized`.
    """
    now = ts if ts is not None else int(time.time())
    stmt = (
        t.insert()
        .values(
            provider="anthropic",
            mode="oauth_claude",
            display_name=display_name,
            base_url=None,
            is_active=0,
            oauth_status="pending",
            created_at=now,
            updated_at=now,
            user_id=user_id,
        )
        .returning(*t.c)
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        row = result.first()
    if row is None:
        raise RuntimeError("insert into llm_credentials ... RETURNING returned no row")
    return _row_to_credential(row)


async def update_oauth_authorized(
    engine: AsyncEngine,
    *,
    user_id: int,
    cred_id: int,
    ciphertext: EncryptedBlob,
    authorized_at: int,
    ts: int | None = None,
) -> LlmCredential | None:
    now = ts if ts is not None else int(time.time())
    stmt = (
        t.update()
        .where(t.c.id == cred_id)
        .values(
            oauth_status="authorized",
            oauth_authorized_at=authorized_at,
            oauth_iv=ciphertext.iv,
            oauth_tag=ciphertext.tag,
            oauth_data=ciphertext.data,
            updated_at=now,
        )
        .returning(*t.c)
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        row = result.first()
    return _row_to_credential(row) if row is not None else None


async def get(
    engine: AsyncEngine, cred_id: int, *, user_id: int
) -> LlmCredential | None:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(select(t).where(t.c.id == cred_id))
        row = result.first()
    return _row_to_credential(row) if row is not None else None


async def get_active(
    engine: AsyncEngine, *, user_id: int
) -> LlmCredential | None:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(select(t).where(t.c.is_active.is_(True)))
        row = result.first()
    return _row_to_credential(row) if row is not None else None


async def get_active_model(
    engine: AsyncEngine, *, user_id: int
) -> str | None:
    """Return the active credential's `model` (or None when no credential
    is active / the active one inherits from `settings.model`). Used by
    the chat routes to pick the per-request `model` before calling the
    agent loop."""
    active = await get_active(engine, user_id=user_id)
    return active.model if active is not None else None


async def list_all(
    engine: AsyncEngine, *, user_id: int
) -> list[LlmCredential]:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t).order_by(desc(t.c.created_at), desc(t.c.id))
        )
        rows = result.all()
    return [_row_to_credential(r) for r in rows]


async def delete(engine: AsyncEngine, cred_id: int, *, user_id: int) -> bool:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(t.delete().where(t.c.id == cred_id))
    return (result.rowcount or 0) > 0


async def set_model(
    engine: AsyncEngine, cred_id: int, model: str | None, *, user_id: int
) -> LlmCredential | None:
    """Update the preferred model on a credential. `None` clears it back
    to the env-var fallback (`settings.model`)."""
    now = int(time.time())
    stmt = (
        t.update()
        .where(t.c.id == cred_id)
        .values(model=model, updated_at=now)
        .returning(*t.c)
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        row = result.first()
    return _row_to_credential(row) if row is not None else None


async def activate(engine: AsyncEngine, cred_id: int, *, user_id: int) -> bool:
    """Set `is_active=1` on the target row, clear it on all others
    belonging to this user.

    Both writes go in the same transaction so the partial unique index
    on (user_id, is_active=1) never sees a transient state with two
    active rows. Returns False if the target row doesn't exist.
    """
    now = int(time.time())
    async with tx_for_user(engine, user_id=user_id) as conn:
        # Deactivate everything else first. WHERE id != X keeps the noisy
        # UPDATE off the target row. RLS already scopes to this user.
        await conn.execute(
            t.update()
            .where(t.c.id != cred_id)
            .where(t.c.is_active.is_(True))
            .values(is_active=False, updated_at=now)
        )
        result = await conn.execute(
            t.update()
            .where(t.c.id == cred_id)
            .values(is_active=True, updated_at=now)
        )
    return (result.rowcount or 0) > 0
