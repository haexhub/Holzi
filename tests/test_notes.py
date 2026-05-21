from pathlib import Path

import aiosqlite
import pytest

from hermes.db import init_db
from hermes.repository import notes


@pytest.fixture
async def conn(tmp_path: Path):
    connection = await init_db(str(tmp_path / "hermes.db"))
    try:
        yield connection
    finally:
        await connection.close()


async def test_upsert_inserts_new_note(conn: aiosqlite.Connection) -> None:
    note = await notes.upsert(
        conn,
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


async def test_upsert_updates_existing_note_keeping_id(conn: aiosqlite.Connection) -> None:
    first = await notes.upsert(conn, key="status", content="v1", ts=1000)
    second = await notes.upsert(conn, key="status", content="v2", ts=2000)
    assert first.id == second.id
    assert second.content == "v2"
    assert second.updated_at == 2000

    fetched = await notes.get(conn, "status")
    assert fetched is not None
    assert fetched.content == "v2"


async def test_get_returns_none_for_missing_key(conn: aiosqlite.Connection) -> None:
    assert await notes.get(conn, "does-not-exist") is None


async def test_find_searches_via_fts_on_content(conn: aiosqlite.Connection) -> None:
    await notes.upsert(conn, key="a", content="reschedule the standup", ts=1)
    await notes.upsert(conn, key="b", content="buy milk", ts=2)
    await notes.upsert(conn, key="c", content="cancel the standup", ts=3)

    hits = await notes.find(conn, query="standup")
    assert {n.key for n in hits} == {"a", "c"}


async def test_find_matches_against_tags(conn: aiosqlite.Connection) -> None:
    await notes.upsert(conn, key="x", content="something", tags="urgent", ts=1)
    await notes.upsert(conn, key="y", content="something else", tags="later", ts=2)

    hits = await notes.find(conn, query="urgent")
    assert [n.key for n in hits] == ["x"]


async def test_fts_index_updates_on_upsert_overwrite(conn: aiosqlite.Connection) -> None:
    await notes.upsert(conn, key="k", content="zorblax", ts=1)
    await notes.upsert(conn, key="k", content="quux", ts=2)

    assert await notes.find(conn, query="zorblax") == []
    hits = await notes.find(conn, query="quux")
    assert [n.key for n in hits] == ["k"]
