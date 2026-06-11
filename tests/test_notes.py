from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import notes


async def test_upsert_inserts_new_note(conn: AsyncEngine) -> None:
    note = await notes.upsert(
        conn,
        user_id=1,
        key="project.holzi.status",
        content="phase 2 in progress",
        tags="hermes,status",
        ts=1000,
    )
    assert note.id > 0
    assert note.key == "project.holzi.status"
    assert note.content == "phase 2 in progress"
    assert note.tags == "hermes,status"
    assert note.updated_at == 1000


async def test_upsert_updates_existing_note_keeping_id(conn: AsyncEngine) -> None:
    first = await notes.upsert(conn, user_id=1, key="status", content="v1", ts=1000)
    second = await notes.upsert(conn, user_id=1, key="status", content="v2", ts=2000)
    assert first.id == second.id
    assert second.content == "v2"
    assert second.updated_at == 2000

    fetched = await notes.get(conn, "status", user_id=1)
    assert fetched is not None
    assert fetched.content == "v2"


async def test_get_returns_none_for_missing_key(conn: AsyncEngine) -> None:
    assert await notes.get(conn, "does-not-exist", user_id=1) is None


async def test_find_searches_via_fts_on_content(conn: AsyncEngine) -> None:
    await notes.upsert(conn, user_id=1, key="a", content="reschedule the standup", ts=1)
    await notes.upsert(conn, user_id=1, key="b", content="buy milk", ts=2)
    await notes.upsert(conn, user_id=1, key="c", content="cancel the standup", ts=3)

    hits = await notes.find(conn, user_id=1, query="standup")
    assert {n.key for n in hits} == {"a", "c"}


async def test_find_matches_against_tags(conn: AsyncEngine) -> None:
    await notes.upsert(conn, user_id=1, key="x", content="something", tags="urgent", ts=1)
    await notes.upsert(conn, user_id=1, key="y", content="something else", tags="later", ts=2)

    hits = await notes.find(conn, user_id=1, query="urgent")
    assert [n.key for n in hits] == ["x"]


async def test_fts_index_updates_on_upsert_overwrite(conn: AsyncEngine) -> None:
    await notes.upsert(conn, user_id=1, key="k", content="zorblax", ts=1)
    await notes.upsert(conn, user_id=1, key="k", content="quux", ts=2)

    assert await notes.find(conn, user_id=1, query="zorblax") == []
    hits = await notes.find(conn, user_id=1, query="quux")
    assert [n.key for n in hits] == ["k"]


async def test_delete_removes_note(conn: AsyncEngine) -> None:
    await notes.upsert(conn, user_id=1, key="gone", content="bye", ts=1)
    assert await notes.delete(conn, "gone", user_id=1) is True
    assert await notes.get(conn, "gone", user_id=1) is None
    # Deleting a missing key is a no-op.
    assert await notes.delete(conn, "gone", user_id=1) is False


# --- Wave C1: cross-user isolation -----------------------------------------
# The `conn` fixture seeds user 1; these tests add user 2 so the
# `notes.user_id` FK holds, then assert one user can never see, search, or
# delete another's notes — and that the same key can collide across users on
# a fresh DB (per-user UniqueConstraint, not global).
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


async def test_get_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    await notes.upsert(conn, user_id=2, key="theirs", content="secret", ts=1)
    # Another user's note is invisible.
    assert await notes.get(conn, "theirs", user_id=1) is None
    assert await notes.get(conn, "theirs", user_id=2) is not None


async def test_list_all_filters_by_user(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    await notes.upsert(conn, user_id=1, key="mine", content="a", ts=1)
    await notes.upsert(conn, user_id=2, key="theirs", content="b", ts=2)
    rows = await notes.list_all(conn, user_id=1)
    assert [n.key for n in rows] == ["mine"]


async def test_find_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    await notes.upsert(conn, user_id=2, key="theirs", content="dentist appointment", ts=1)
    # User 1's search must not surface user 2's note.
    assert await notes.find(conn, user_id=1, query="dentist") == []
    hits = await notes.find(conn, user_id=2, query="dentist")
    assert [n.key for n in hits] == ["theirs"]


async def test_delete_is_scoped_to_owner(conn: AsyncEngine) -> None:
    await _seed_two_users(conn)
    await notes.upsert(conn, user_id=2, key="theirs", content="keep", ts=1)
    # user 1 can't delete user 2's note.
    assert await notes.delete(conn, "theirs", user_id=1) is False
    # Row untouched and still owned by user 2.
    still = await notes.get(conn, "theirs", user_id=2)
    assert still is not None
    assert still.content == "keep"


async def test_same_key_can_collide_across_users(conn: AsyncEngine) -> None:
    # On a fresh DB `key` is unique PER USER, so both users can hold "foo".
    await _seed_two_users(conn)
    a = await notes.upsert(conn, user_id=1, key="foo", content="mine", ts=1)
    b = await notes.upsert(conn, user_id=2, key="foo", content="theirs", ts=2)
    assert a.id != b.id
    assert (await notes.get(conn, "foo", user_id=1)).content == "mine"
    assert (await notes.get(conn, "foo", user_id=2)).content == "theirs"
