"""Unit tests for the personas repository (Plan 29-A → Plan 36 fragments).

Plan 36 split the single `prompt` column into three fragments
(`soul`, `identity`, `agents`) and wired every `create`/`update` to
append an atomic `persona_history` snapshot. These tests cover both
the field-shape change and the auto-snapshot contract.
"""
import json

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import persona_history as history_repo
from hermes.repository import personas as repo


@pytest.mark.asyncio
async def test_list_empty_on_fresh_db(conn: AsyncEngine) -> None:
    assert await repo.list_all(conn, user_id=1) == []
    assert await repo.get_default(conn, user_id=1) is None


@pytest.mark.asyncio
async def test_create_basic_persona(conn: AsyncEngine) -> None:
    p = await repo.create(
        conn,
        user_id=1,
        name="Hermes",
        soul="be calm",
        identity="be direct",
        agents="use tools",
        is_default=True,
    )
    assert p.id > 0
    assert p.name == "Hermes"
    assert p.soul == "be calm"
    assert p.identity == "be direct"
    assert p.agents == "use tools"
    assert p.is_default is True
    assert p.created_at > 0
    assert p.updated_at == p.created_at

    # create() must have written exactly one history row capturing the
    # new persona's three fragments (and NOT is_default).
    history = await history_repo.list_for_persona(conn, p.id)
    assert len(history) == 1
    body = json.loads(history[0].snapshot_json)
    assert body == {
        "name": "Hermes",
        "soul": "be calm",
        "identity": "be direct",
        "agents": "use tools",
    }
    assert history[0].author == "user"


@pytest.mark.asyncio
async def test_create_with_history_author_overrides_default(
    conn: AsyncEngine,
) -> None:
    """Plan 36 Task 5: the lifespan backfill seeds the default persona
    with ``history_author='system'`` so the initial audit row is
    distinguishable from user-edits. The kwarg defaults to ``'user'``
    (covered by `test_create_basic_persona`); this verifies the
    override flows through to the history row.
    """
    p = await repo.create(
        conn,
        user_id=1,
        name="Seeded",
        soul="",
        identity="seed",
        agents="",
        is_default=True,
        history_author="system",
    )
    history = await history_repo.list_for_persona(conn, p.id)
    assert len(history) == 1
    assert history[0].author == "system"


@pytest.mark.asyncio
async def test_create_duplicate_name_raises(conn: AsyncEngine) -> None:
    await repo.create(
        conn,
        user_id=1,
        name="Hermes",
        soul="",
        identity="x",
        agents="",
        is_default=True,
    )
    with pytest.raises(IntegrityError):
        await repo.create(
            conn,
            user_id=1,
            name="Hermes",
            soul="",
            identity="y",
            agents="",
            is_default=False,
        )


@pytest.mark.asyncio
async def test_get_and_get_by_name(conn: AsyncEngine) -> None:
    created = await repo.create(
        conn,
        user_id=1,
        name="Sokrates",
        soul="",
        identity="Ask questions.",
        agents="",
        is_default=False,
    )
    fetched = await repo.get(conn, created.id, user_id=1)
    assert fetched is not None
    assert fetched.name == "Sokrates"
    assert fetched.identity == "Ask questions."

    by_name = await repo.get_by_name(conn, "Sokrates", user_id=1)
    assert by_name is not None and by_name.id == created.id

    assert await repo.get_by_name(conn, "missing", user_id=1) is None
    assert await repo.get(conn, 99999, user_id=1) is None


@pytest.mark.asyncio
async def test_single_default_trigger_on_insert(conn: AsyncEngine) -> None:
    """Inserting a new is_default=1 row demotes any existing default."""
    first = await repo.create(
        conn, user_id=1, name="A", soul="", identity="a", agents="", is_default=True
    )
    second = await repo.create(
        conn, user_id=1, name="B", soul="", identity="b", agents="", is_default=True
    )

    refreshed_first = await repo.get(conn, first.id, user_id=1)
    refreshed_second = await repo.get(conn, second.id, user_id=1)
    assert refreshed_first is not None and refreshed_first.is_default is False
    assert refreshed_second is not None and refreshed_second.is_default is True

    default = await repo.get_default(conn, user_id=1)
    assert default is not None and default.id == second.id


@pytest.mark.asyncio
async def test_single_default_trigger_on_update(conn: AsyncEngine) -> None:
    first = await repo.create(
        conn, user_id=1, name="A", soul="", identity="a", agents="", is_default=True
    )
    second = await repo.create(
        conn, user_id=1, name="B", soul="", identity="b", agents="", is_default=False
    )

    updated = await repo.update(conn, second.id, user_id=1, is_default=True)
    assert updated is not None and updated.is_default is True

    refreshed_first = await repo.get(conn, first.id, user_id=1)
    assert refreshed_first is not None and refreshed_first.is_default is False


@pytest.mark.asyncio
async def test_update_partial_fields(conn: AsyncEngine) -> None:
    p = await repo.create(
        conn,
        user_id=1,
        name="A",
        soul="old-soul",
        identity="old-id",
        agents="old-ag",
        is_default=True,
    )
    updated = await repo.update(conn, p.id, user_id=1, identity="new-id")
    assert updated is not None
    assert updated.identity == "new-id"
    # Unchanged fragments stay put.
    assert updated.soul == "old-soul"
    assert updated.agents == "old-ag"
    assert updated.name == "A"
    assert updated.is_default is True
    assert updated.updated_at >= p.updated_at

    # create() + one update() ⇒ two history rows total.
    history = await history_repo.list_for_persona(conn, p.id)
    assert len(history) == 2
    # Newest-first ordering: row[0] is the post-update snapshot.
    body = json.loads(history[0].snapshot_json)
    assert body == {
        "name": "A",
        "soul": "old-soul",
        "identity": "new-id",
        "agents": "old-ag",
    }


@pytest.mark.asyncio
async def test_update_missing_returns_none(conn: AsyncEngine) -> None:
    assert await repo.update(conn, 99999, user_id=1, identity="x") is None


@pytest.mark.asyncio
async def test_delete_default_returns_false(conn: AsyncEngine) -> None:
    p = await repo.create(
        conn, user_id=1, name="A", soul="", identity="a", agents="", is_default=True
    )
    deleted = await repo.delete(conn, p.id, user_id=1)
    assert deleted is False
    # Row must still exist.
    assert await repo.get(conn, p.id, user_id=1) is not None


@pytest.mark.asyncio
async def test_delete_non_default_returns_true(conn: AsyncEngine) -> None:
    default = await repo.create(
        conn, user_id=1, name="A", soul="", identity="a", agents="", is_default=True
    )
    other = await repo.create(
        conn, user_id=1, name="B", soul="", identity="b", agents="", is_default=False
    )

    deleted = await repo.delete(conn, other.id, user_id=1)
    assert deleted is True
    assert await repo.get(conn, other.id, user_id=1) is None
    # The default persona survives.
    assert await repo.get(conn, default.id, user_id=1) is not None

    # FK CASCADE wipes history rows for the deleted persona.
    assert await history_repo.list_for_persona(conn, other.id) == []


@pytest.mark.asyncio
async def test_delete_missing_returns_false(conn: AsyncEngine) -> None:
    assert await repo.delete(conn, 99999, user_id=1) is False


@pytest.mark.asyncio
async def test_list_all_orders_default_first_then_by_name(
    conn: AsyncEngine,
) -> None:
    await repo.create(
        conn, user_id=1, name="Zeta", soul="", identity="z", agents="", is_default=False
    )
    await repo.create(
        conn, user_id=1, name="Alpha", soul="", identity="a", agents="", is_default=False
    )
    await repo.create(
        conn, user_id=1, name="Beta", soul="", identity="b", agents="", is_default=True
    )

    rows = await repo.list_all(conn, user_id=1)
    assert [r.name for r in rows] == ["Beta", "Alpha", "Zeta"]


# --- Wave C1: cross-user isolation -----------------------------------------
# The `conn` fixture seeds user 1; these tests add user 2 so the
# `personas.user_id` FK holds, then assert one user can never see, update, or
# delete another's personas, and that each user keeps an INDEPENDENT default
# (proving the per-user single-default trigger).
async def _seed_two_users(conn: AsyncEngine) -> None:
    from sqlalchemy import text

    async with conn.begin() as db:
        await db.execute(
            text(
                "INSERT OR IGNORE INTO users(id, role, bootstrap_completed, "
                "created_at) VALUES (1,'admin',0,0)"
            )
        )
        await db.execute(
            text(
                "INSERT OR IGNORE INTO users(id, role, bootstrap_completed, "
                "created_at) VALUES (2,'member',0,0)"
            )
        )


@pytest.mark.asyncio
async def test_get_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    theirs = await repo.create(
        conn, user_id=2, name="Theirs", soul="", identity="t", agents="", is_default=True
    )
    # Another user's persona is invisible by id and by name.
    assert await repo.get(conn, theirs.id, user_id=1) is None
    assert await repo.get_by_name(conn, "Theirs", user_id=1) is None
    assert await repo.get(conn, theirs.id, user_id=2) is not None


@pytest.mark.asyncio
async def test_list_all_filters_by_user(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    mine = await repo.create(
        conn, user_id=1, name="Mine", soul="", identity="m", agents="", is_default=True
    )
    await repo.create(
        conn, user_id=2, name="Theirs", soul="", identity="t", agents="", is_default=True
    )
    rows = await repo.list_all(conn, user_id=1)
    assert [r.id for r in rows] == [mine.id]


@pytest.mark.asyncio
async def test_update_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    theirs = await repo.create(
        conn, user_id=2, name="Theirs", soul="", identity="t", agents="", is_default=True
    )
    # user 1 can't update user 2's persona (no-op → None).
    assert (
        await repo.update(conn, theirs.id, user_id=1, identity="hijacked") is None
    )
    # Row untouched and still owned by user 2.
    still = await repo.get(conn, theirs.id, user_id=2)
    assert still is not None and still.identity == "t"


@pytest.mark.asyncio
async def test_delete_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    # A non-default persona of user 2 — user 1 still can't delete it.
    await repo.create(
        conn, user_id=2, name="TheirDefault", soul="", identity="d", agents="",
        is_default=True,
    )
    theirs = await repo.create(
        conn, user_id=2, name="Theirs", soul="", identity="t", agents="",
        is_default=False,
    )
    assert await repo.delete(conn, theirs.id, user_id=1) is False
    assert await repo.get(conn, theirs.id, user_id=2) is not None


@pytest.mark.asyncio
async def test_each_user_has_independent_default(conn: AsyncEngine) -> None:
    """Per-user single-default trigger: setting user 2's default must NOT
    unset user 1's. This is the core Wave C1 persona invariant."""
    await _seed_two_users(conn)
    mine = await repo.create(
        conn, user_id=1, name="Mine", soul="", identity="m", agents="", is_default=True
    )
    theirs = await repo.create(
        conn, user_id=2, name="Theirs", soul="", identity="t", agents="", is_default=True
    )

    # Both users keep their own default — neither demoted the other.
    my_default = await repo.get_default(conn, user_id=1)
    their_default = await repo.get_default(conn, user_id=2)
    assert my_default is not None and my_default.id == mine.id
    assert their_default is not None and their_default.id == theirs.id

    # Promoting a NEW default for user 2 demotes only user 2's old default.
    theirs2 = await repo.create(
        conn, user_id=2, name="Theirs2", soul="", identity="t2", agents="",
        is_default=True,
    )
    assert (await repo.get_default(conn, user_id=2)).id == theirs2.id
    # user 1's default is still intact.
    assert (await repo.get_default(conn, user_id=1)).id == mine.id


@pytest.mark.asyncio
async def test_same_name_allowed_across_users(conn: AsyncEngine) -> None:
    """Names are unique PER user — two users may both own a persona named
    'Hermes' (composite UniqueConstraint on fresh DBs)."""
    await _seed_two_users(conn)
    await repo.create(
        conn, user_id=1, name="Hermes", soul="", identity="a", agents="", is_default=True
    )
    # Same name under user 2 must NOT raise.
    p2 = await repo.create(
        conn, user_id=2, name="Hermes", soul="", identity="b", agents="", is_default=True
    )
    assert p2.id > 0
    assert (await repo.get_by_name(conn, "Hermes", user_id=2)).id == p2.id
