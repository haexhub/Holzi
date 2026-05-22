from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.repository import conversations


async def test_create_returns_conversation_with_id_and_timestamps(
    conn: AsyncConnection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1700000000)
    assert convo.id > 0
    assert convo.channel == "signal"
    assert convo.started_at == 1700000000
    assert convo.updated_at == 1700000000
    assert convo.title is None
    assert convo.external_id is None


async def test_create_persists_optional_fields(conn: AsyncConnection) -> None:
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


async def test_get_returns_none_for_missing_id(conn: AsyncConnection) -> None:
    assert await conversations.get(conn, 99999) is None


async def test_list_by_channel_filters_and_orders_by_updated_desc(
    conn: AsyncConnection,
) -> None:
    a = await conversations.create(conn, channel="signal", ts=1000)
    b = await conversations.create(conn, channel="web", ts=2000)
    c = await conversations.create(conn, channel="signal", ts=3000)

    signal_convos = await conversations.list_by_channel(conn, "signal")
    assert [x.id for x in signal_convos] == [c.id, a.id]

    web_convos = await conversations.list_by_channel(conn, "web")
    assert [x.id for x in web_convos] == [b.id]


async def test_touch_updates_updated_at_only(conn: AsyncConnection) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await conversations.touch(conn, convo.id, ts=2500)
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.started_at == 1000
    assert fetched.updated_at == 2500


async def test_list_all_returns_every_channel_in_updated_desc(
    conn: AsyncConnection,
) -> None:
    a = await conversations.create(conn, channel="signal", ts=1000)
    b = await conversations.create(conn, channel="web", ts=3000)
    c = await conversations.create(conn, channel="vscode", ts=2000)

    all_convos = await conversations.list_all(conn)
    assert [x.id for x in all_convos] == [b.id, c.id, a.id]


async def test_list_all_can_filter_by_channel_and_since(
    conn: AsyncConnection,
) -> None:
    a = await conversations.create(conn, channel="signal", ts=1000)
    b = await conversations.create(conn, channel="signal", ts=3000)
    web = await conversations.create(conn, channel="web", ts=2500)

    only_signal = await conversations.list_all(conn, channel="signal")
    assert {c.id for c in only_signal} == {a.id, b.id}

    recent = await conversations.list_all(conn, since_unix=2500)
    assert {c.id for c in recent} == {b.id, web.id}


async def test_message_count_returns_zero_for_empty_conversation(
    conn: AsyncConnection,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    assert await conversations.message_count(conn, convo.id) == 0


async def test_message_count_counts_only_target_conversation(
    conn: AsyncConnection,
) -> None:
    from hermes.repository import messages

    a = await conversations.create(conn, channel="signal", ts=1)
    b = await conversations.create(conn, channel="web", ts=2)
    await messages.append(conn, conversation_id=a.id, role="user", content="x", ts=10)
    await messages.append(conn, conversation_id=a.id, role="assistant", content="y", ts=11)
    await messages.append(conn, conversation_id=b.id, role="user", content="z", ts=12)

    assert await conversations.message_count(conn, a.id) == 2
    assert await conversations.message_count(conn, b.id) == 1
