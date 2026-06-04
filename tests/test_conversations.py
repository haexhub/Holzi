from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.config import settings
from hermes.repository import conversations, messages

_DAY = 86_400


async def test_create_returns_conversation_with_id_and_timestamps(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="task", ts=1700000000)
    assert convo.id > 0
    assert convo.channel == "task"
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
    a = await conversations.create(conn, channel="task", ts=1000)
    b = await conversations.create(conn, channel="web", ts=2000)
    c = await conversations.create(conn, channel="task", ts=3000)

    signal_convos = await conversations.list_by_channel(conn, "task")
    assert [x.id for x in signal_convos] == [c.id, a.id]

    web_convos = await conversations.list_by_channel(conn, "web")
    assert [x.id for x in web_convos] == [b.id]


async def test_touch_updates_updated_at_only(conn: AsyncEngine) -> None:
    convo = await conversations.create(conn, channel="task", ts=1000)
    await conversations.touch(conn, convo.id, ts=2500)
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.started_at == 1000
    assert fetched.updated_at == 2500


async def test_list_all_returns_every_channel_in_updated_desc(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="task", ts=1000)
    b = await conversations.create(conn, channel="web", ts=3000)
    c = await conversations.create(conn, channel="vscode", ts=2000)

    all_convos = await conversations.list_all(conn)
    assert [x.id for x in all_convos] == [b.id, c.id, a.id]


async def test_list_all_can_filter_by_channel_and_since(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="task", ts=1000)
    b = await conversations.create(conn, channel="task", ts=3000)
    web = await conversations.create(conn, channel="web", ts=2500)

    only_signal = await conversations.list_all(conn, channel="task")
    assert {c.id for c in only_signal} == {a.id, b.id}

    recent = await conversations.list_all(conn, since_unix=2500)
    assert {c.id for c in recent} == {b.id, web.id}


async def test_find_latest_by_external_id_returns_most_recent(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(
        conn, channel="task", external_id="tg:42", ts=1000
    )
    b = await conversations.create(
        conn, channel="task", external_id="tg:42", ts=3000
    )
    # different chat — must not be returned
    await conversations.create(conn, channel="task", external_id="tg:99", ts=4000)

    found = await conversations.find_latest_by_external_id(
        conn, channel="task", external_id="tg:42"
    )
    assert found is not None
    assert found.id == b.id
    assert a.id != b.id  # sanity


async def test_find_latest_by_external_id_returns_none_when_no_match(
    conn: AsyncEngine,
) -> None:
    await conversations.create(
        conn, channel="task", external_id="tg:1", ts=1000
    )
    assert (
        await conversations.find_latest_by_external_id(
            conn, channel="task", external_id="tg:2"
        )
        is None
    )


async def test_find_latest_by_external_id_scopes_by_channel(
    conn: AsyncEngine,
) -> None:
    """Same external_id under a different channel must not bleed through."""
    await conversations.create(
        conn, channel="task", external_id="tg:42", ts=1000
    )
    assert (
        await conversations.find_latest_by_external_id(
            conn, channel="web", external_id="tg:42"
        )
        is None
    )


async def test_message_count_returns_zero_for_empty_conversation(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="task", ts=1000)
    assert await conversations.message_count(conn, convo.id) == 0


async def test_message_count_counts_only_target_conversation(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="task", ts=1)
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
        conn, channel="task", title="standup", ts=2000
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


async def test_search_multi_token_uses_or_semantics_for_messages(
    conn: AsyncEngine,
) -> None:
    """Multi-word queries should OR across tokens on both sides — a thread
    that mentions only ONE of the words still surfaces, matching the title
    LIKE behaviour and how chat search elsewhere feels.
    """
    only_dentist = await conversations.create(
        conn, channel="web", title="t1", ts=1000
    )
    only_appt = await conversations.create(conn, channel="web", title="t2", ts=2000)
    await messages.append(
        conn,
        conversation_id=only_dentist.id,
        role="user",
        content="reschedule the dentist",
        ts=1500,
    )
    await messages.append(
        conn,
        conversation_id=only_appt.id,
        role="user",
        content="set up an appointment",
        ts=2500,
    )

    results = await conversations.search(conn, query="dentist appointment")
    ids = {c.id for c in results}
    assert only_dentist.id in ids
    assert only_appt.id in ids


async def test_search_message_prefix_match(conn: AsyncEngine) -> None:
    """Typing a partial word like ``dent`` should find a message mentioning
    ``dentist`` — FTS5 prefix matching on each token.
    """
    convo = await conversations.create(conn, channel="web", title="t", ts=1000)
    await messages.append(
        conn,
        conversation_id=convo.id,
        role="user",
        content="reschedule the dentist",
        ts=1500,
    )

    results = await conversations.search(conn, query="dent")
    assert [c.id for c in results] == [convo.id]


async def test_search_pure_operator_query_returns_empty(
    conn: AsyncEngine,
) -> None:
    """A non-blank query with no word characters (e.g. ``***``) is treated
    as "I searched, found nothing" — not as a request for the full list.
    """
    await conversations.create(conn, channel="web", title="hello", ts=1000)
    assert await conversations.search(conn, query="***") == []


# ---------------------------------------------------------------------------
# Retention: bookmark + TTL + sweep + scratch dir.
# ---------------------------------------------------------------------------


async def test_create_sets_expires_at_from_ttl(conn: AsyncEngine) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    expected = 1000 + settings.conversation_ttl_days * _DAY
    assert convo.bookmarked is False
    assert convo.expires_at == expected
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.expires_at == expected


async def test_create_bookmarked_has_null_expires_at(conn: AsyncEngine) -> None:
    convo = await conversations.create(
        conn, channel="web", ts=1000, bookmarked=True
    )
    assert convo.bookmarked is True
    assert convo.expires_at is None


async def test_touch_refreshes_expires_at_for_non_bookmarked(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    await conversations.touch(conn, convo.id, ts=5000)
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.updated_at == 5000
    assert fetched.expires_at == 5000 + settings.conversation_ttl_days * _DAY


async def test_touch_keeps_bookmarked_expires_at_null(conn: AsyncEngine) -> None:
    convo = await conversations.create(
        conn, channel="web", ts=1000, bookmarked=True
    )
    await conversations.touch(conn, convo.id, ts=5000)
    fetched = await conversations.get(conn, convo.id)
    assert fetched is not None
    assert fetched.updated_at == 5000
    assert fetched.expires_at is None


async def test_update_title_refreshes_expires_at(conn: AsyncEngine) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    updated = await conversations.update_title(
        conn, convo.id, title="renamed", ts=5000
    )
    assert updated is not None
    assert updated.expires_at == 5000 + settings.conversation_ttl_days * _DAY


async def test_set_bookmarked_clears_and_restores_expires_at(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    assert convo.expires_at is not None

    pinned = await conversations.set_bookmarked(
        conn, convo.id, bookmarked=True, ts=2000
    )
    assert pinned is not None
    assert pinned.bookmarked is True
    assert pinned.expires_at is None

    unpinned = await conversations.set_bookmarked(
        conn, convo.id, bookmarked=False, ts=10_000
    )
    assert unpinned is not None
    assert unpinned.bookmarked is False
    # Unbookmarking should re-arm the clock from `ts`, not the original
    # updated_at, so stale rows don't immediately disappear.
    assert unpinned.expires_at == 10_000 + settings.conversation_ttl_days * _DAY


async def test_set_bookmarked_unknown_id_returns_none(conn: AsyncEngine) -> None:
    assert (
        await conversations.set_bookmarked(conn, 99999, bookmarked=True) is None
    )


async def test_list_expired_only_returns_past_non_bookmarked(
    conn: AsyncEngine,
) -> None:
    # Expired: created long ago, TTL window has passed.
    expired = await conversations.create(conn, channel="web", ts=0)
    # Not yet expired.
    fresh = await conversations.create(conn, channel="web", ts=10_000_000)
    # Bookmarked — expires_at is NULL, must never appear.
    pinned = await conversations.create(
        conn, channel="web", ts=0, bookmarked=True
    )

    # Sweep "now" is 1 second past expired's expires_at.
    now = expired.expires_at + 1  # type: ignore[operator]
    rows = await conversations.list_expired(conn, now=now)
    ids = {r.id for r in rows}
    assert expired.id in ids
    assert fresh.id not in ids
    assert pinned.id not in ids


async def test_sweep_expired_deletes_expired_and_keeps_bookmarked(
    conn: AsyncEngine, tmp_path: Path
) -> None:
    from hermes.repository import messages

    expired = await conversations.create(conn, channel="web", ts=0)
    await messages.append(
        conn, conversation_id=expired.id, role="user", content="dies", ts=1
    )
    pinned = await conversations.create(
        conn, channel="web", ts=0, bookmarked=True
    )
    fresh = await conversations.create(conn, channel="web", ts=10_000_000)

    scratch_root = tmp_path / "conversations"
    scratch_root.mkdir()
    expired_dir = scratch_root / str(expired.id)
    expired_dir.mkdir()
    (expired_dir / "upload.bin").write_bytes(b"x")
    pinned_dir = scratch_root / str(pinned.id)
    pinned_dir.mkdir()

    now = expired.expires_at + 1  # type: ignore[operator]
    deleted = await conversations.sweep_expired(
        conn, now=now, scratch_root=scratch_root
    )

    assert deleted == [expired.id]
    assert await conversations.get(conn, expired.id) is None
    assert await conversations.get(conn, pinned.id) is not None
    assert await conversations.get(conn, fresh.id) is not None
    # Scratch dir for the deleted conversation is gone, others survive.
    assert not expired_dir.exists()
    assert pinned_dir.exists()


async def test_delete_removes_scratch_dir(
    conn: AsyncEngine, tmp_path: Path
) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    scratch_root = tmp_path / "conversations"
    scratch_root.mkdir()
    scratch = scratch_root / str(convo.id)
    scratch.mkdir()
    (scratch / "tool.out").write_text("hello")

    assert await conversations.delete(
        conn, convo.id, scratch_root=scratch_root
    )
    assert not scratch.exists()


async def test_delete_without_scratch_root_is_safe(conn: AsyncEngine) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    assert await conversations.delete(conn, convo.id) is True
    assert await conversations.get(conn, convo.id) is None


async def test_delete_missing_scratch_dir_is_noop(
    conn: AsyncEngine, tmp_path: Path
) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    # Note: never created the scratch dir.
    assert await conversations.delete(
        conn, convo.id, scratch_root=tmp_path / "conversations"
    )
