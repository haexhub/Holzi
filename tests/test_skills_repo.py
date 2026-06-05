"""Unit tests for the skills repository (Plan 33 → Plan 37)."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import skills as repo


@pytest.mark.asyncio
async def test_list_empty_on_fresh_db(conn: AsyncEngine) -> None:
    assert await repo.list_all(conn) == []


@pytest.mark.asyncio
async def test_create_basic_skill(conn: AsyncEngine) -> None:
    s = await repo.create(
        conn,
        slug="strict-german",
        name="Strict German",
        description="Reply in German only.",
        when_to_use="immer",
        body_markdown="Antworte ausschließlich auf Deutsch.",
    )
    assert s.id > 0
    assert s.slug == "strict-german"
    assert s.name == "Strict German"
    assert s.description == "Reply in German only."
    assert s.when_to_use == "immer"
    assert s.body_markdown == "Antworte ausschließlich auf Deutsch."
    assert s.enabled is True
    assert s.created_at > 0
    assert s.updated_at == s.created_at


@pytest.mark.asyncio
async def test_create_without_when_to_use(conn: AsyncEngine) -> None:
    s = await repo.create(
        conn,
        slug="brief",
        name="Brief",
        description="Stay short.",
        body_markdown="Max 3 Sätze.",
    )
    assert s.when_to_use == ""


@pytest.mark.asyncio
async def test_create_disabled(conn: AsyncEngine) -> None:
    s = await repo.create(
        conn,
        slug="hidden",
        name="Hidden",
        description="Off",
        body_markdown="body",
        enabled=False,
    )
    assert s.enabled is False


@pytest.mark.asyncio
async def test_create_duplicate_slug_raises(conn: AsyncEngine) -> None:
    await repo.create(conn, slug="x", name="X", description="x", body_markdown="x")
    with pytest.raises(IntegrityError):
        await repo.create(conn, slug="x", name="Other", description="other", body_markdown="other")


@pytest.mark.asyncio
async def test_get_and_get_by_slug(conn: AsyncEngine) -> None:
    s = await repo.create(conn, slug="alpha", name="Alpha", description="a", body_markdown="a")
    fetched = await repo.get(conn, s.id)
    assert fetched is not None and fetched.slug == "alpha"

    by_slug = await repo.get_by_slug(conn, "alpha")
    assert by_slug is not None and by_slug.id == s.id

    assert await repo.get_by_slug(conn, "missing") is None
    assert await repo.get(conn, 99999) is None


@pytest.mark.asyncio
async def test_update_partial_fields(conn: AsyncEngine) -> None:
    s = await repo.create(conn, slug="a", name="A", description="old", body_markdown="old body")
    updated = await repo.update(conn, s.id, body_markdown="new body")
    assert updated is not None
    assert updated.body_markdown == "new body"
    assert updated.description == "old"
    assert updated.slug == "a"
    assert updated.updated_at >= s.updated_at


@pytest.mark.asyncio
async def test_update_enabled_flag(conn: AsyncEngine) -> None:
    s = await repo.create(conn, slug="tog", name="T", description="t", body_markdown="t")
    assert s.enabled is True
    updated = await repo.update(conn, s.id, enabled=False)
    assert updated is not None
    assert updated.enabled is False
    back = await repo.update(conn, s.id, enabled=True)
    assert back is not None
    assert back.enabled is True


@pytest.mark.asyncio
async def test_update_missing_returns_none(conn: AsyncEngine) -> None:
    assert await repo.update(conn, 99999, body_markdown="x") is None


@pytest.mark.asyncio
async def test_delete_returns_true_then_false(conn: AsyncEngine) -> None:
    s = await repo.create(conn, slug="a", name="A", description="a", body_markdown="a")
    assert await repo.delete(conn, s.id) is True
    assert await repo.get(conn, s.id) is None
    assert await repo.delete(conn, s.id) is False


@pytest.mark.asyncio
async def test_list_all_orders_by_slug(conn: AsyncEngine) -> None:
    await repo.create(conn, slug="zeta", name="Zeta", description="z", body_markdown="z")
    await repo.create(conn, slug="alpha", name="Alpha", description="a", body_markdown="a")
    rows = await repo.list_all(conn)
    assert [r.slug for r in rows] == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_list_enabled_excludes_disabled(conn: AsyncEngine) -> None:
    await repo.create(
        conn, slug="active", name="Active", description="a", body_markdown="a", enabled=True
    )
    await repo.create(
        conn, slug="hidden", name="Hidden", description="h", body_markdown="h", enabled=False
    )
    enabled = await repo.list_enabled(conn)
    assert [r.slug for r in enabled] == ["active"]


@pytest.mark.asyncio
async def test_list_enabled_alphabetical(conn: AsyncEngine) -> None:
    await repo.create(conn, slug="zeta", name="Zeta", description="z", body_markdown="z")
    await repo.create(conn, slug="alpha", name="Alpha", description="a", body_markdown="a")
    enabled = await repo.list_enabled(conn)
    assert [r.slug for r in enabled] == ["alpha", "zeta"]
