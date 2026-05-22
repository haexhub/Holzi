import json

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import conversations, messages, notes
from hermes.tools.memory import build_memory_tools


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool not found: {name}")


# ----------------------------------------------------------------------------
# build_memory_tools catalog shape
# ----------------------------------------------------------------------------
async def test_build_memory_tools_returns_expected_catalog(
    conn: AsyncEngine,
) -> None:
    catalog = build_memory_tools(conn)
    names = {t.name for t in catalog}
    assert names == {
        "recall_memory",
        "list_conversations",
        "get_conversation",
        "save_note",
        "get_note",
        "find_notes",
    }


# ----------------------------------------------------------------------------
# recall_memory
# ----------------------------------------------------------------------------
async def test_recall_memory_finds_messages_and_notes(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1)
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="reschedule standup", ts=10
    )
    await messages.append(
        conn, conversation_id=convo.id, role="user", content="buy milk", ts=20
    )
    await notes.upsert(conn, key="standup.tips", content="rotate facilitator weekly", ts=5)
    await notes.upsert(conn, key="other", content="unrelated", ts=6)

    tool = _by_name(build_memory_tools(conn), "recall_memory")
    payload = await tool.handler({"query": "standup"})
    data = json.loads(payload)

    assert any(m["content"] == "reschedule standup" for m in data["messages"])
    assert {n["key"] for n in data["notes"]} == {"standup.tips"}


async def test_recall_memory_respects_limit(conn: AsyncEngine) -> None:
    convo = await conversations.create(conn, channel="signal", ts=1)
    for i in range(5):
        await messages.append(
            conn, conversation_id=convo.id, role="user", content="alpha", ts=i + 10
        )

    tool = _by_name(build_memory_tools(conn), "recall_memory")
    payload = await tool.handler({"query": "alpha", "limit": 2})
    data = json.loads(payload)
    assert len(data["messages"]) == 2


# ----------------------------------------------------------------------------
# list_conversations
# ----------------------------------------------------------------------------
async def test_list_conversations_returns_all_with_message_count(
    conn: AsyncEngine,
) -> None:
    a = await conversations.create(conn, channel="signal", title="A", ts=1)
    b = await conversations.create(conn, channel="web", title="B", ts=3)
    await messages.append(conn, conversation_id=a.id, role="user", content="x", ts=10)
    await messages.append(conn, conversation_id=a.id, role="assistant", content="y", ts=11)
    await messages.append(conn, conversation_id=b.id, role="user", content="z", ts=12)

    tool = _by_name(build_memory_tools(conn), "list_conversations")
    payload = await tool.handler({})
    data = json.loads(payload)

    by_id = {c["id"]: c for c in data}
    assert by_id[a.id]["message_count"] == 2
    assert by_id[b.id]["message_count"] == 1
    assert by_id[a.id]["channel"] == "signal"
    assert by_id[a.id]["title"] == "A"


async def test_list_conversations_can_filter_by_channel(
    conn: AsyncEngine,
) -> None:
    await conversations.create(conn, channel="signal", ts=1)
    await conversations.create(conn, channel="web", ts=2)
    await conversations.create(conn, channel="signal", ts=3)

    tool = _by_name(build_memory_tools(conn), "list_conversations")
    payload = await tool.handler({"channel": "signal"})
    data = json.loads(payload)
    assert {c["channel"] for c in data} == {"signal"}
    assert len(data) == 2


# ----------------------------------------------------------------------------
# get_conversation
# ----------------------------------------------------------------------------
async def test_get_conversation_returns_metadata_and_messages(
    conn: AsyncEngine,
) -> None:
    convo = await conversations.create(conn, channel="signal", title="t", ts=1)
    await messages.append(conn, conversation_id=convo.id, role="user", content="a", ts=10)
    await messages.append(conn, conversation_id=convo.id, role="assistant", content="b", ts=11)

    tool = _by_name(build_memory_tools(conn), "get_conversation")
    payload = await tool.handler({"id": convo.id})
    data = json.loads(payload)

    assert data["conversation"]["id"] == convo.id
    assert data["conversation"]["title"] == "t"
    assert [m["content"] for m in data["messages"]] == ["a", "b"]


async def test_get_conversation_returns_error_for_missing_id(
    conn: AsyncEngine,
) -> None:
    tool = _by_name(build_memory_tools(conn), "get_conversation")
    payload = await tool.handler({"id": 99999})
    data = json.loads(payload)
    assert "error" in data


# ----------------------------------------------------------------------------
# save_note / get_note / find_notes
# ----------------------------------------------------------------------------
async def test_save_note_upserts_and_returns_payload(
    conn: AsyncEngine,
) -> None:
    tool = _by_name(build_memory_tools(conn), "save_note")
    payload = await tool.handler(
        {"key": "k1", "content": "v1", "tags": ["a", "b"]}
    )
    data = json.loads(payload)
    assert data["key"] == "k1"
    assert data["content"] == "v1"
    assert data["tags"] == "a,b"

    # Upsert again with new content
    payload2 = await tool.handler({"key": "k1", "content": "v2"})
    data2 = json.loads(payload2)
    assert data2["id"] == data["id"]
    assert data2["content"] == "v2"


async def test_get_note_returns_null_for_missing(
    conn: AsyncEngine,
) -> None:
    tool = _by_name(build_memory_tools(conn), "get_note")
    payload = await tool.handler({"key": "does-not-exist"})
    assert json.loads(payload) is None


async def test_get_note_returns_stored_note(
    conn: AsyncEngine,
) -> None:
    await notes.upsert(conn, key="k", content="hello", tags="x,y", ts=1)
    tool = _by_name(build_memory_tools(conn), "get_note")
    data = json.loads(await tool.handler({"key": "k"}))
    assert data["content"] == "hello"
    assert data["tags"] == "x,y"


async def test_find_notes_returns_matches(conn: AsyncEngine) -> None:
    await notes.upsert(conn, key="a", content="standup notes", ts=1)
    await notes.upsert(conn, key="b", content="grocery", ts=2)

    tool = _by_name(build_memory_tools(conn), "find_notes")
    data = json.loads(await tool.handler({"query": "standup"}))
    assert {n["key"] for n in data} == {"a"}


async def test_find_notes_filters_by_tags(conn: AsyncEngine) -> None:
    await notes.upsert(conn, key="a", content="something", tags="urgent,work", ts=1)
    await notes.upsert(conn, key="b", content="something else", tags="urgent", ts=2)
    await notes.upsert(conn, key="c", content="another", tags="later", ts=3)

    tool = _by_name(build_memory_tools(conn), "find_notes")
    data = json.loads(await tool.handler({"query": "something", "tags": ["work"]}))
    assert {n["key"] for n in data} == {"a"}
