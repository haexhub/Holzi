"""Unit tests for the skills repository (Plan 33)."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import personas as personas_repo
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
    assert s.created_at > 0
    assert s.updated_at == s.created_at


@pytest.mark.asyncio
async def test_create_without_when_to_use(conn: AsyncEngine) -> None:
    s = await repo.create(
        conn,
        slug="brief",
        name="Brief",
        description="Stay short.",
        when_to_use=None,
        body_markdown="Max 3 Sätze.",
    )
    assert s.when_to_use is None


@pytest.mark.asyncio
async def test_create_duplicate_slug_raises(conn: AsyncEngine) -> None:
    await repo.create(
        conn,
        slug="x",
        name="X",
        description="x",
        when_to_use=None,
        body_markdown="x",
    )
    with pytest.raises(IntegrityError):
        await repo.create(
            conn,
            slug="x",
            name="Other",
            description="other",
            when_to_use=None,
            body_markdown="other",
        )


@pytest.mark.asyncio
async def test_get_and_get_by_slug(conn: AsyncEngine) -> None:
    s = await repo.create(
        conn,
        slug="alpha",
        name="Alpha",
        description="a",
        when_to_use=None,
        body_markdown="a",
    )
    fetched = await repo.get(conn, s.id)
    assert fetched is not None and fetched.slug == "alpha"

    by_slug = await repo.get_by_slug(conn, "alpha")
    assert by_slug is not None and by_slug.id == s.id

    assert await repo.get_by_slug(conn, "missing") is None
    assert await repo.get(conn, 99999) is None


@pytest.mark.asyncio
async def test_update_partial_fields(conn: AsyncEngine) -> None:
    s = await repo.create(
        conn,
        slug="a",
        name="A",
        description="old",
        when_to_use=None,
        body_markdown="old body",
    )
    updated = await repo.update(conn, s.id, body_markdown="new body")
    assert updated is not None
    assert updated.body_markdown == "new body"
    assert updated.description == "old"
    assert updated.slug == "a"  # slug cannot change via update
    assert updated.updated_at >= s.updated_at


@pytest.mark.asyncio
async def test_update_missing_returns_none(conn: AsyncEngine) -> None:
    assert await repo.update(conn, 99999, body_markdown="x") is None


@pytest.mark.asyncio
async def test_delete_returns_true_then_false(conn: AsyncEngine) -> None:
    s = await repo.create(
        conn,
        slug="a",
        name="A",
        description="a",
        when_to_use=None,
        body_markdown="a",
    )
    assert await repo.delete(conn, s.id) is True
    assert await repo.get(conn, s.id) is None
    assert await repo.delete(conn, s.id) is False


@pytest.mark.asyncio
async def test_list_all_orders_by_name(conn: AsyncEngine) -> None:
    await repo.create(
        conn,
        slug="zeta",
        name="Zeta",
        description="z",
        when_to_use=None,
        body_markdown="z",
    )
    await repo.create(
        conn,
        slug="alpha",
        name="Alpha",
        description="a",
        when_to_use=None,
        body_markdown="a",
    )
    rows = await repo.list_all(conn)
    assert [r.slug for r in rows] == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_list_for_persona_empty(conn: AsyncEngine) -> None:
    persona = await personas_repo.create(
        conn, name="P", soul="", identity="p", agents="", is_default=True
    )
    assert await repo.list_for_persona(conn, persona.id) == []


@pytest.mark.asyncio
async def test_set_persona_skills_inserts_in_order(conn: AsyncEngine) -> None:
    persona = await personas_repo.create(
        conn, name="P", soul="", identity="p", agents="", is_default=True
    )
    s1 = await repo.create(
        conn,
        slug="one",
        name="One",
        description="1",
        when_to_use=None,
        body_markdown="1",
    )
    s2 = await repo.create(
        conn,
        slug="two",
        name="Two",
        description="2",
        when_to_use=None,
        body_markdown="2",
    )

    await repo.set_persona_skills(
        conn,
        persona.id,
        [
            {"skill_id": s2.id, "ordering": 0, "enabled": True},
            {"skill_id": s1.id, "ordering": 1, "enabled": False},
        ],
    )

    rows = await repo.list_for_persona(conn, persona.id)
    assert len(rows) == 2
    skill_a, order_a, enabled_a = rows[0]
    skill_b, order_b, enabled_b = rows[1]
    assert skill_a.slug == "two"
    assert order_a == 0
    assert enabled_a is True
    assert skill_b.slug == "one"
    assert order_b == 1
    assert enabled_b is False


@pytest.mark.asyncio
async def test_set_persona_skills_is_atomic_replacement(
    conn: AsyncEngine,
) -> None:
    """set_persona_skills replaces the entire list for that persona."""
    persona = await personas_repo.create(
        conn, name="P", soul="", identity="p", agents="", is_default=True
    )
    s1 = await repo.create(
        conn,
        slug="one",
        name="One",
        description="1",
        when_to_use=None,
        body_markdown="1",
    )
    s2 = await repo.create(
        conn,
        slug="two",
        name="Two",
        description="2",
        when_to_use=None,
        body_markdown="2",
    )

    await repo.set_persona_skills(
        conn,
        persona.id,
        [{"skill_id": s1.id, "ordering": 0, "enabled": True}],
    )
    assert len(await repo.list_for_persona(conn, persona.id)) == 1

    await repo.set_persona_skills(
        conn,
        persona.id,
        [{"skill_id": s2.id, "ordering": 0, "enabled": True}],
    )
    rows = await repo.list_for_persona(conn, persona.id)
    assert len(rows) == 1
    assert rows[0][0].slug == "two"


@pytest.mark.asyncio
async def test_set_persona_skills_empty_clears_all(conn: AsyncEngine) -> None:
    persona = await personas_repo.create(
        conn, name="P", soul="", identity="p", agents="", is_default=True
    )
    s = await repo.create(
        conn,
        slug="one",
        name="One",
        description="1",
        when_to_use=None,
        body_markdown="1",
    )
    await repo.set_persona_skills(
        conn,
        persona.id,
        [{"skill_id": s.id, "ordering": 0, "enabled": True}],
    )
    await repo.set_persona_skills(conn, persona.id, [])
    assert await repo.list_for_persona(conn, persona.id) == []


@pytest.mark.asyncio
async def test_set_persona_skills_atomic_rollback_on_invalid_skill(
    conn: AsyncEngine,
) -> None:
    """Invalid skill_id in the items list rolls back the whole replacement."""
    persona = await personas_repo.create(
        conn, name="P", soul="", identity="p", agents="", is_default=True
    )
    s = await repo.create(
        conn,
        slug="one",
        name="One",
        description="1",
        when_to_use=None,
        body_markdown="1",
    )
    await repo.set_persona_skills(
        conn,
        persona.id,
        [{"skill_id": s.id, "ordering": 0, "enabled": True}],
    )

    with pytest.raises(IntegrityError):
        await repo.set_persona_skills(
            conn,
            persona.id,
            [{"skill_id": 99999, "ordering": 0, "enabled": True}],
        )

    rows = await repo.list_for_persona(conn, persona.id)
    assert len(rows) == 1
    assert rows[0][0].slug == "one"


@pytest.mark.asyncio
async def test_delete_skill_cascades_persona_skills(
    conn: AsyncEngine,
) -> None:
    persona = await personas_repo.create(
        conn, name="P", soul="", identity="p", agents="", is_default=True
    )
    s = await repo.create(
        conn,
        slug="one",
        name="One",
        description="1",
        when_to_use=None,
        body_markdown="1",
    )
    await repo.set_persona_skills(
        conn,
        persona.id,
        [{"skill_id": s.id, "ordering": 0, "enabled": True}],
    )

    await repo.delete(conn, s.id)
    assert await repo.list_for_persona(conn, persona.id) == []


@pytest.mark.asyncio
async def test_delete_persona_cascades_persona_skills(
    conn: AsyncEngine,
) -> None:
    default = await personas_repo.create(
        conn, name="D", soul="", identity="d", agents="", is_default=True
    )
    other = await personas_repo.create(
        conn, name="O", soul="", identity="o", agents="", is_default=False
    )
    s = await repo.create(
        conn,
        slug="one",
        name="One",
        description="1",
        when_to_use=None,
        body_markdown="1",
    )
    await repo.set_persona_skills(
        conn,
        other.id,
        [{"skill_id": s.id, "ordering": 0, "enabled": True}],
    )

    await personas_repo.delete(conn, other.id)
    # Skill itself survives; the row in persona_skills is gone.
    assert await repo.get(conn, s.id) is not None
    assert await repo.list_for_persona(conn, other.id) == []
    # Default still alive.
    assert await personas_repo.get(conn, default.id) is not None
