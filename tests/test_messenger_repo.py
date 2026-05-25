"""Unit tests for the messenger_accounts repository functions."""
import pytest

from hermes.repository import messenger as repo


@pytest.mark.asyncio
async def test_list_empty_on_fresh_db(conn) -> None:
    assert await repo.list_all(conn) == []
    assert await repo.get_active(conn, "signal") is None
    assert await repo.get_active(conn, "telegram") is None


@pytest.mark.asyncio
async def test_create_signal_starts_inactive(conn) -> None:
    account = await repo.create_signal(conn, "+491701234567")
    assert account.provider == "signal"
    assert account.phone_number == "+491701234567"
    assert account.is_active is False
    assert account.bot_username is None
    assert account.bot_token_iv is None


@pytest.mark.asyncio
async def test_get_by_phone_finds_signal_only(conn) -> None:
    await repo.create_signal(conn, "+491701234567")
    found = await repo.get_by_phone(conn, "+491701234567")
    assert found is not None
    assert found.provider == "signal"
    assert await repo.get_by_phone(conn, "+491700000000") is None


@pytest.mark.asyncio
async def test_activate_deactivates_same_provider_siblings(conn) -> None:
    first = await repo.create_signal(conn, "+491701111111")
    second = await repo.create_signal(conn, "+491702222222")

    activated_first = await repo.activate(conn, first.id)
    assert activated_first is not None and activated_first.is_active is True
    active = await repo.get_active(conn, "signal")
    assert active is not None and active.id == first.id

    activated_second = await repo.activate(conn, second.id)
    assert activated_second is not None and activated_second.is_active is True
    active = await repo.get_active(conn, "signal")
    assert active is not None and active.id == second.id

    # first must now be inactive — partial unique index would have
    # tripped on the second activate() otherwise, so this is also a
    # smoke test on schema.sql.
    first_after = await repo.get_by_id(conn, first.id)
    assert first_after is not None and first_after.is_active is False


@pytest.mark.asyncio
async def test_activate_telegram_and_signal_can_coexist(conn) -> None:
    """Partial unique index keys on (provider) → one active per provider."""
    signal_account = await repo.create_signal(conn, "+491701234567")
    telegram_account = await repo.create_telegram(
        conn,
        bot_username="holzi_bot",
        bot_token_iv="aa" * 12,
        bot_token_tag="bb" * 16,
        bot_token_data="cc" * 24,
    )
    await repo.activate(conn, signal_account.id)
    await repo.activate(conn, telegram_account.id)

    assert (await repo.get_active(conn, "signal")).id == signal_account.id
    assert (await repo.get_active(conn, "telegram")).id == telegram_account.id


@pytest.mark.asyncio
async def test_delete_removes_row(conn) -> None:
    account = await repo.create_signal(conn, "+491701234567")
    assert await repo.delete(conn, account.id) is True
    assert await repo.delete(conn, account.id) is False
    assert await repo.get_by_id(conn, account.id) is None


@pytest.mark.asyncio
async def test_activate_missing_id_returns_none(conn) -> None:
    assert await repo.activate(conn, 99999) is None
