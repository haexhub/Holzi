import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import conversations, messages


@pytest.fixture
async def convo_id(conn: AsyncEngine) -> int:
    convo = await conversations.create(conn, user_id=1, channel="task", ts=1000)
    return convo.id


async def test_append_returns_message_with_id_and_fields(
    conn: AsyncEngine, convo_id: int
) -> None:
    msg = await messages.append(
        conn,
        conversation_id=convo_id,
        role="user",
        content="hello world",
        ts=1100,
    )
    assert msg.id > 0
    assert msg.conversation_id == convo_id
    assert msg.role == "user"
    assert msg.content == "hello world"
    assert msg.ts == 1100
    assert msg.meta_json is None


async def test_list_by_conversation_returns_messages_in_chronological_order(
    conn: AsyncEngine, convo_id: int
) -> None:
    a = await messages.append(conn, conversation_id=convo_id, role="user", content="first", ts=10)
    b = await messages.append(
        conn, conversation_id=convo_id, role="assistant", content="second", ts=20
    )
    c = await messages.append(conn, conversation_id=convo_id, role="user", content="third", ts=30)

    listed = await messages.list_by_conversation(conn, convo_id)
    assert [m.id for m in listed] == [a.id, b.id, c.id]


async def test_list_by_conversation_does_not_leak_other_conversations(
    conn: AsyncEngine,
) -> None:
    convo_a = await conversations.create(conn, user_id=1, channel="task", ts=1)
    convo_b = await conversations.create(conn, user_id=1, channel="web", ts=2)
    await messages.append(conn, conversation_id=convo_a.id, role="user", content="A", ts=10)
    await messages.append(conn, conversation_id=convo_b.id, role="user", content="B", ts=20)

    a_msgs = await messages.list_by_conversation(conn, convo_a.id)
    assert [m.content for m in a_msgs] == ["A"]


async def test_fts_search_finds_matching_messages(
    conn: AsyncEngine, convo_id: int
) -> None:
    await messages.append(
        conn, conversation_id=convo_id, role="user", content="reschedule the meeting", ts=10
    )
    await messages.append(conn, conversation_id=convo_id, role="user", content="buy milk", ts=20)
    await messages.append(
        conn, conversation_id=convo_id, role="user", content="cancel the meeting", ts=30
    )

    hits = await messages.fts_search(conn, query="meeting")
    assert {m.content for m in hits} == {"reschedule the meeting", "cancel the meeting"}


async def test_fts_search_can_be_scoped_to_conversation(
    conn: AsyncEngine,
) -> None:
    convo_a = await conversations.create(conn, user_id=1, channel="task", ts=1)
    convo_b = await conversations.create(conn, user_id=1, channel="web", ts=2)
    await messages.append(
        conn, conversation_id=convo_a.id, role="user", content="meeting at noon", ts=10
    )
    await messages.append(
        conn, conversation_id=convo_b.id, role="user", content="meeting tomorrow", ts=20
    )

    hits = await messages.fts_search(conn, query="meeting", conversation_id=convo_a.id)
    assert [m.content for m in hits] == ["meeting at noon"]


async def test_fts_index_updates_when_message_is_deleted(
    conn: AsyncEngine, convo_id: int
) -> None:
    msg = await messages.append(
        conn, conversation_id=convo_id, role="user", content="zorblax", ts=10
    )
    from sqlalchemy import text as _text

    async with conn.begin() as raw:
        await raw.execute(
            _text("DELETE FROM messages WHERE id = :id"), {"id": msg.id}
        )

    hits = await messages.fts_search(conn, query="zorblax")
    assert hits == []


async def test_last_user_message_returns_most_recent_user_turn(
    conn: AsyncEngine, convo_id: int
) -> None:
    await messages.append(conn, conversation_id=convo_id, role="user", content="first", ts=10)
    await messages.append(
        conn, conversation_id=convo_id, role="assistant", content="reply", ts=20
    )
    last = await messages.append(
        conn, conversation_id=convo_id, role="user", content="second", ts=30
    )
    await messages.append(
        conn, conversation_id=convo_id, role="assistant", content="reply 2", ts=40
    )

    found = await messages.last_user_message(conn, convo_id)
    assert found is not None
    assert found.id == last.id
    assert found.content == "second"


async def test_last_user_message_returns_none_when_no_user_turn(
    conn: AsyncEngine, convo_id: int
) -> None:
    await messages.append(
        conn, conversation_id=convo_id, role="assistant", content="orphan", ts=10
    )
    assert await messages.last_user_message(conn, convo_id) is None


async def test_delete_after_removes_trailing_messages(
    conn: AsyncEngine, convo_id: int
) -> None:
    await messages.append(conn, conversation_id=convo_id, role="user", content="q", ts=10)
    user = await messages.append(
        conn, conversation_id=convo_id, role="user", content="ask again", ts=20
    )
    await messages.append(
        conn, conversation_id=convo_id, role="assistant", content="tool call", ts=30
    )
    await messages.append(conn, conversation_id=convo_id, role="tool", content="result", ts=40)
    await messages.append(
        conn, conversation_id=convo_id, role="assistant", content="answer", ts=50
    )

    deleted = await messages.delete_after(conn, convo_id, after_id=user.id)
    assert deleted == 3

    remaining = await messages.list_by_conversation(conn, convo_id)
    assert [(m.role, m.content) for m in remaining] == [
        ("user", "q"),
        ("user", "ask again"),
    ]


async def test_delete_after_keeps_fts_index_in_sync(
    conn: AsyncEngine, convo_id: int
) -> None:
    user = await messages.append(
        conn, conversation_id=convo_id, role="user", content="keep me", ts=10
    )
    await messages.append(
        conn, conversation_id=convo_id, role="assistant", content="zorblax", ts=20
    )

    await messages.delete_after(conn, convo_id, after_id=user.id)

    assert await messages.fts_search(conn, query="zorblax") == []
    assert len(await messages.fts_search(conn, query="keep")) == 1


async def test_delete_after_is_scoped_to_conversation(conn: AsyncEngine) -> None:
    convo_a = await conversations.create(conn, user_id=1, channel="web", ts=1)
    convo_b = await conversations.create(conn, user_id=1, channel="web", ts=2)
    user_a = await messages.append(
        conn, conversation_id=convo_a.id, role="user", content="a-user", ts=10
    )
    await messages.append(
        conn, conversation_id=convo_a.id, role="assistant", content="a-reply", ts=20
    )
    await messages.append(
        conn, conversation_id=convo_b.id, role="assistant", content="b-reply", ts=30
    )

    deleted = await messages.delete_after(conn, convo_a.id, after_id=user_a.id)
    assert deleted == 1
    b_msgs = await messages.list_by_conversation(conn, convo_b.id)
    assert [m.content for m in b_msgs] == ["b-reply"]


async def test_get_returns_message_by_id(conn: AsyncEngine, convo_id: int) -> None:
    msg = await messages.append(
        conn, conversation_id=convo_id, role="user", content="find me", ts=10
    )
    found = await messages.get(conn, msg.id)
    assert found is not None
    assert found.id == msg.id
    assert (found.role, found.content, found.ts) == ("user", "find me", 10)


async def test_get_returns_none_for_unknown_id(conn: AsyncEngine) -> None:
    assert await messages.get(conn, 99999) is None


async def test_update_content_replaces_text_and_keeps_position(
    conn: AsyncEngine, convo_id: int
) -> None:
    msg = await messages.append(
        conn, conversation_id=convo_id, role="user", content="old text", ts=10
    )
    updated = await messages.update_content(conn, msg.id, content="new text")
    assert updated is not None
    assert updated.content == "new text"
    # Role and timestamp are preserved so the edited turn stays in place.
    assert (updated.role, updated.ts) == ("user", 10)


async def test_update_content_keeps_fts_index_in_sync(
    conn: AsyncEngine, convo_id: int
) -> None:
    msg = await messages.append(
        conn, conversation_id=convo_id, role="user", content="zorblax", ts=10
    )
    await messages.update_content(conn, msg.id, content="quuxified")
    assert await messages.fts_search(conn, query="zorblax") == []
    assert len(await messages.fts_search(conn, query="quuxified")) == 1


async def test_update_content_returns_none_for_unknown_id(conn: AsyncEngine) -> None:
    assert await messages.update_content(conn, 99999, content="x") is None
