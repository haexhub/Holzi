"""CRUD for `messenger_accounts`.

Signal rows carry only `phone_number` — the linking secret stays in
signal-cli's data volume. Telegram rows carry the AES-GCM ciphertext of
the bot token (hex strings, same pattern as llm_credentials.api_key_*).

The "at most one active per provider" semantics are enforced by a
partial unique index on `(provider) WHERE is_active = 1` — see
schema.sql. `activate()` deactivates same-provider siblings in the same
transaction so the index never trips.
"""
import time

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import MessengerAccount
from hermes.schema import messenger_accounts as t


def _row_to_account(row) -> MessengerAccount:
    return MessengerAccount(
        id=row.id,
        provider=row.provider,
        is_active=bool(row.is_active),
        phone_number=row.phone_number,
        bot_username=row.bot_username,
        bot_token_iv=row.bot_token_iv,
        bot_token_tag=row.bot_token_tag,
        bot_token_data=row.bot_token_data,
        allowed_chat_ids=row.allowed_chat_ids,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_all(engine: AsyncEngine) -> list[MessengerAccount]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(t).order_by(desc(t.c.id)))).all()
    return [_row_to_account(r) for r in rows]


async def get_by_id(engine: AsyncEngine, account_id: int) -> MessengerAccount | None:
    async with engine.connect() as conn:
        row = (await conn.execute(select(t).where(t.c.id == account_id))).first()
    return _row_to_account(row) if row else None


async def get_active(engine: AsyncEngine, provider: str) -> MessengerAccount | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(t).where(t.c.provider == provider, t.c.is_active == 1)
            )
        ).first()
    return _row_to_account(row) if row else None


async def get_by_phone(engine: AsyncEngine, phone_number: str) -> MessengerAccount | None:
    """Lookup used by the signal-link-poll flow to spot a freshly-linked
    number that's already known to us (i.e. a re-link of the same number)."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(t).where(
                    t.c.provider == "signal", t.c.phone_number == phone_number
                )
            )
        ).first()
    return _row_to_account(row) if row else None


async def create_signal(engine: AsyncEngine, phone_number: str) -> MessengerAccount:
    """Insert a fresh signal row. Inactive by default — the caller
    activates it explicitly after the link flow succeeds."""
    now = int(time.time())
    stmt = (
        t.insert()
        .values(
            provider="signal",
            is_active=0,
            phone_number=phone_number,
            created_at=now,
            updated_at=now,
        )
        .returning(*t.c)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(stmt)).first()
    if row is None:
        raise RuntimeError("insert into messenger_accounts ... RETURNING returned no row")
    return _row_to_account(row)


async def create_telegram(
    engine: AsyncEngine,
    *,
    bot_username: str,
    bot_token_iv: str,
    bot_token_tag: str,
    bot_token_data: str,
    allowed_chat_ids: str | None = None,
) -> MessengerAccount:
    now = int(time.time())
    stmt = (
        t.insert()
        .values(
            provider="telegram",
            is_active=0,
            bot_username=bot_username,
            bot_token_iv=bot_token_iv,
            bot_token_tag=bot_token_tag,
            bot_token_data=bot_token_data,
            allowed_chat_ids=allowed_chat_ids,
            created_at=now,
            updated_at=now,
        )
        .returning(*t.c)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(stmt)).first()
    if row is None:
        raise RuntimeError("insert into messenger_accounts ... RETURNING returned no row")
    return _row_to_account(row)


async def activate(engine: AsyncEngine, account_id: int) -> MessengerAccount | None:
    """Set `is_active=1` for this row, `0` for all other rows with the
    same provider. Single transaction — the partial unique index forbids
    a mid-transaction state where two same-provider rows are active."""
    now = int(time.time())
    async with engine.begin() as conn:
        target = (
            await conn.execute(select(t.c.provider).where(t.c.id == account_id))
        ).first()
        if target is None:
            return None
        # Order matters with the partial unique index: deactivate first.
        await conn.execute(
            update(t)
            .where(t.c.provider == target.provider, t.c.id != account_id)
            .values(is_active=0, updated_at=now)
        )
        await conn.execute(
            update(t).where(t.c.id == account_id).values(is_active=1, updated_at=now)
        )
    return await get_by_id(engine, account_id)


async def delete(engine: AsyncEngine, account_id: int) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(t.delete().where(t.c.id == account_id))
    return result.rowcount > 0
