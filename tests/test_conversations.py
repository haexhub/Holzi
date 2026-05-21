from pathlib import Path

import aiosqlite
import pytest

from hermes.db import init_db
from hermes.repository import conversations


@pytest.fixture
async def conn(tmp_path: Path):
    connection = await init_db(str(tmp_path / "hermes.db"))
    try:
        yield connection
    finally:
        await connection.close()


async def test_create_returns_conversation_with_id_and_timestamps(
    conn: aiosqlite.Connection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1700000000)
    assert convo.id > 0
    assert convo.channel == "signal"
    assert convo.started_at == 1700000000
    assert convo.updated_at == 1700000000
    assert convo.title is None
    assert convo.external_id is None


async def test_create_persists_optional_fields(conn: aiosqlite.Connection) -> None:
    convo = await conversations.create(
        conn,
        channel="vscode",
        external_id="workspace-42",
        title="Refactor auth",
        ts=1700000000,
    )
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.external_id == "workspace-42"
    assert fetched.title == "Refactor auth"
    assert fetched.channel == "vscode"


async def test_get_returns_none_for_missing_id(conn: aiosqlite.Connection) -> None:
    assert await conversations.get(conn, 99999) is None


async def test_list_by_channel_filters_and_orders_by_updated_desc(
    conn: aiosqlite.Connection,
) -> None:
    a = await conversations.create(conn, channel="signal", ts=1000)
    b = await conversations.create(conn, channel="web", ts=2000)
    c = await conversations.create(conn, channel="signal", ts=3000)

    signal_convos = await conversations.list_by_channel(conn, "signal")
    assert [x.id for x in signal_convos] == [c.id, a.id]

    web_convos = await conversations.list_by_channel(conn, "web")
    assert [x.id for x in web_convos] == [b.id]


async def test_touch_updates_updated_at_only(conn: aiosqlite.Connection) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await conversations.touch(conn, convo.id, ts=2500)
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.started_at == 1000
    assert fetched.updated_at == 2500
