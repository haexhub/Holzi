"""Unit tests for the personas repository (Plan 29-A)."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import personas as repo


@pytest.mark.asyncio
async def test_list_empty_on_fresh_db(conn: AsyncEngine) -> None:
    assert await repo.list_all(conn) == []
    assert await repo.get_default(conn) is None


@pytest.mark.asyncio
async def test_create_basic_persona(conn: AsyncEngine) -> None:
    p = await repo.create(
        conn, name="Hermes", prompt="Be direct.", is_default=True
    )
    assert p.id > 0
    assert p.name == "Hermes"
    assert p.prompt == "Be direct."
    assert p.is_default is True
    assert p.created_at > 0
    assert p.updated_at == p.created_at


@pytest.mark.asyncio
async def test_create_duplicate_name_raises(conn: AsyncEngine) -> None:
    await repo.create(conn, name="Hermes", prompt="x", is_default=True)
    with pytest.raises(IntegrityError):
        await repo.create(conn, name="Hermes", prompt="y", is_default=False)


@pytest.mark.asyncio
async def test_get_and_get_by_name(conn: AsyncEngine) -> None:
    created = await repo.create(
        conn, name="Sokrates", prompt="Ask questions.", is_default=False
    )
    fetched = await repo.get(conn, created.id)
    assert fetched is not None
    assert fetched.name == "Sokrates"

    by_name = await repo.get_by_name(conn, "Sokrates")
    assert by_name is not None and by_name.id == created.id

    assert await repo.get_by_name(conn, "missing") is None
    assert await repo.get(conn, 99999) is None


@pytest.mark.asyncio
async def test_single_default_trigger_on_insert(conn: AsyncEngine) -> None:
    """Inserting a new is_default=1 row demotes any existing default."""
    first = await repo.create(conn, name="A", prompt="a", is_default=True)
    second = await repo.create(conn, name="B", prompt="b", is_default=True)

    refreshed_first = await repo.get(conn, first.id)
    refreshed_second = await repo.get(conn, second.id)
    assert refreshed_first is not None and refreshed_first.is_default is False
    assert refreshed_second is not None and refreshed_second.is_default is True

    default = await repo.get_default(conn)
    assert default is not None and default.id == second.id


@pytest.mark.asyncio
async def test_single_default_trigger_on_update(conn: AsyncEngine) -> None:
    first = await repo.create(conn, name="A", prompt="a", is_default=True)
    second = await repo.create(conn, name="B", prompt="b", is_default=False)

    updated = await repo.update(conn, second.id, is_default=True)
    assert updated is not None and updated.is_default is True

    refreshed_first = await repo.get(conn, first.id)
    assert refreshed_first is not None and refreshed_first.is_default is False


@pytest.mark.asyncio
async def test_update_partial_fields(conn: AsyncEngine) -> None:
    p = await repo.create(conn, name="A", prompt="old", is_default=True)
    updated = await repo.update(conn, p.id, prompt="new")
    assert updated is not None
    assert updated.prompt == "new"
    assert updated.name == "A"
    assert updated.is_default is True
    assert updated.updated_at >= p.updated_at


@pytest.mark.asyncio
async def test_update_missing_returns_none(conn: AsyncEngine) -> None:
    assert await repo.update(conn, 99999, prompt="x") is None


@pytest.mark.asyncio
async def test_delete_default_returns_false(conn: AsyncEngine) -> None:
    p = await repo.create(conn, name="A", prompt="a", is_default=True)
    deleted = await repo.delete(conn, p.id)
    assert deleted is False
    # Row must still exist.
    assert await repo.get(conn, p.id) is not None


@pytest.mark.asyncio
async def test_delete_non_default_returns_true(conn: AsyncEngine) -> None:
    default = await repo.create(conn, name="A", prompt="a", is_default=True)
    other = await repo.create(conn, name="B", prompt="b", is_default=False)

    deleted = await repo.delete(conn, other.id)
    assert deleted is True
    assert await repo.get(conn, other.id) is None
    # The default persona survives.
    assert await repo.get(conn, default.id) is not None


@pytest.mark.asyncio
async def test_delete_missing_returns_false(conn: AsyncEngine) -> None:
    assert await repo.delete(conn, 99999) is False


@pytest.mark.asyncio
async def test_list_all_orders_default_first_then_by_name(
    conn: AsyncEngine,
) -> None:
    await repo.create(conn, name="Zeta", prompt="z", is_default=False)
    await repo.create(conn, name="Alpha", prompt="a", is_default=False)
    await repo.create(conn, name="Beta", prompt="b", is_default=True)

    rows = await repo.list_all(conn)
    assert [r.name for r in rows] == ["Beta", "Alpha", "Zeta"]
