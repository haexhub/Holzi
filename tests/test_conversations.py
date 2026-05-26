from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import conversations


async def test_create_returns_conversation_with_id_and_timestamps(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1700000000)
    assert convo.id > 0
    assert convo.channel == "signal"
    assert convo.started_at == 1700000000
    assert convo.updated_at == 1700000000
    assert convo.title is None
    assert convo.external_id is None


async def test_create_persists_optional_fields(conn: AsyncEngine) -> None:
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


async def test_get_returns_none_for_missing_id(conn: AsyncEngine) -> None:
    assert await conversations.get(conn, 99999) is None


async def test_list_by_channel_filters_and_orders_by_updated_desc(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="signal", ts=1000)
    b = await conversations.create(conn, channel="web", ts=2000)
    c = await conversations.create(conn, channel="signal", ts=3000)

    signal_convos = await conversations.list_by_channel(conn, "signal")
    assert [x.id for x in signal_convos] == [c.id, a.id]

    web_convos = await conversations.list_by_channel(conn, "web")
    assert [x.id for x in web_convos] == [b.id]


async def test_touch_updates_updated_at_only(conn: AsyncEngine) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    await conversations.touch(conn, convo.id, ts=2500)
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.started_at == 1000
    assert fetched.updated_at == 2500


async def test_list_all_returns_every_channel_in_updated_desc(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="signal", ts=1000)
    b = await conversations.create(conn, channel="web", ts=3000)
    c = await conversations.create(conn, channel="vscode", ts=2000)

    all_convos = await conversations.list_all(conn)
    assert [x.id for x in all_convos] == [b.id, c.id, a.id]


async def test_list_all_can_filter_by_channel_and_since(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="signal", ts=1000)
    b = await conversations.create(conn, channel="signal", ts=3000)
    web = await conversations.create(conn, channel="web", ts=2500)

    only_signal = await conversations.list_all(conn, channel="signal")
    assert {c.id for c in only_signal} == {a.id, b.id}

    recent = await conversations.list_all(conn, since_unix=2500)
    assert {c.id for c in recent} == {b.id, web.id}


async def test_find_latest_by_external_id_returns_most_recent(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(
        conn, channel="telegram", external_id="tg:42", ts=1000
    )
    b = await conversations.create(
        conn, channel="telegram", external_id="tg:42", ts=3000
    )
    # different chat — must not be returned
    await conversations.create(conn, channel="telegram", external_id="tg:99", ts=4000)

    found = await conversations.find_latest_by_external_id(
        conn, channel="telegram", external_id="tg:42"
    )
    assert found is not None
    assert found.id == b.id
    assert a.id != b.id  # sanity


async def test_find_latest_by_external_id_returns_none_when_no_match(
    conn: AsyncEngine,
) -> None:
    await conversations.create(
        conn, channel="telegram", external_id="tg:1", ts=1000
    )
    assert (
        await conversations.find_latest_by_external_id(
            conn, channel="telegram", external_id="tg:2"
        )
        is None
    )


async def test_find_latest_by_external_id_scopes_by_channel(
    conn: AsyncEngine,
) -> None:
    """Same external_id under a different channel must not bleed through."""
    await conversations.create(
        conn, channel="signal", external_id="tg:42", ts=1000
    )
    assert (
        await conversations.find_latest_by_external_id(
            conn, channel="telegram", external_id="tg:42"
        )
        is None
    )


async def test_message_count_returns_zero_for_empty_conversation(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1000)
    assert await conversations.message_count(conn, convo.id) == 0


async def test_message_count_counts_only_target_conversation(
    conn: AsyncEngine,
) -> None:
    from hermes.repository import messages

    a = await conversations.create(conn, channel="signal", ts=1)
    b = await conversations.create(conn, channel="web", ts=2)
    await messages.append(conn, conversation_id=a.id, role="user", content="x", ts=10)
    await messages.append(conn, conversation_id=a.id, role="assistant", content="y", ts=11)
    await messages.append(conn, conversation_id=b.id, role="user", content="z", ts=12)

    assert await conversations.message_count(conn, a.id) == 2
    assert await conversations.message_count(conn, b.id) == 1


async def test_search_finds_by_title_substring(conn: AsyncEngine) -> None:
    hit = await conversations.create(
        conn, channel="web", title="Refactor auth", ts=1000
    )
    await conversations.create(conn, channel="web", title="Plan groceries", ts=2000)

    results = await conversations.search(conn, query="refactor")
    assert [c.id for c in results] == [hit.id]


async def test_search_finds_by_message_content(conn: AsyncEngine) -> None:
    from hermes.repository import messages

    convo = await conversations.create(conn, channel="web", title="t", ts=1000)
    await messages.append(
        conn,
        conversation_id=convo.id,
        role="user",
        content="reschedule the dentist",
        ts=1500,
    )

    results = await conversations.search(conn, query="dentist")
    assert [c.id for c in results] == [convo.id]


async def test_search_dedupes_title_and_message_hits(conn: AsyncEngine) -> None:
    from hermes.repository import messages

    convo = await conversations.create(
        conn, channel="web", title="dentist visit", ts=1000
    )
    await messages.append(
        conn,
        conversation_id=convo.id,
        role="user",
        content="dentist confirmed",
        ts=1500,
    )

    results = await conversations.search(conn, query="dentist")
    assert [c.id for c in results] == [convo.id]


async def test_search_can_filter_by_channel(conn: AsyncEngine) -> None:
    web = await conversations.create(
        conn, channel="web", title="standup", ts=1000
    )
    await conversations.create(
        conn, channel="signal", title="standup", ts=2000
    )

    results = await conversations.search(conn, query="standup", channel="web")
    assert [c.id for c in results] == [web.id]


async def test_search_returns_empty_for_no_hits(conn: AsyncEngine) -> None:
    await conversations.create(conn, channel="web", title="hello", ts=1000)
    assert await conversations.search(conn, query="zzznomatchzzz") == []


async def test_search_orders_results_newest_first(conn: AsyncEngine) -> None:
    older = await conversations.create(
        conn, channel="web", title="payroll", ts=1000
    )
    newer = await conversations.create(
        conn, channel="web", title="payroll", ts=5000
    )

    results = await conversations.search(conn, query="payroll")
    assert [c.id for c in results] == [newer.id, older.id]


async def test_search_blank_query_falls_back_to_list_all(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="web", ts=1000)
    b = await conversations.create(conn, channel="web", ts=2000)

    results = await conversations.search(conn, query="   ")
    assert [c.id for c in results] == [b.id, a.id]


async def test_search_strips_fts_operators(conn: AsyncEngine) -> None:
    """User input with FTS5-meaningful characters must not raise a SQL error."""
    convo = await conversations.create(
        conn, channel="web", title="payroll review", ts=1000
    )
    results = await conversations.search(conn, query='"payroll*"')
    assert [c.id for c in results] == [convo.id]
