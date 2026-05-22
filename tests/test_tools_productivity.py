import json

from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.tools.productivity import build_productivity_tools


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool not found: {name}")


async def test_build_productivity_tools_catalog(
    conn: AsyncConnection,
) -> None:
    catalog = build_productivity_tools(conn)
    assert {t.name for t in catalog} == {
        "reminder_set",
        "reminder_list",
        "todo_add",
        "todo_list",
        "todo_done",
    }


# ---------------------------------------------------------------------------
# reminder_set / reminder_list
# ---------------------------------------------------------------------------
async def test_reminder_set_creates_reminder(conn: AsyncConnection) -> None:
    tool = _by_name(build_productivity_tools(conn), "reminder_set")
    out = await tool.handler({"due_at": 2000, "message": "standup"})
    data = json.loads(out)
    assert data["due_at"] == 2000
    assert data["message"] == "standup"
    assert data["channel"] == "signal"


async def test_reminder_set_rejects_missing_due_at(
    conn: AsyncConnection,
) -> None:
    tool = _by_name(build_productivity_tools(conn), "reminder_set")
    data = json.loads(await tool.handler({"message": "x"}))
    assert "error" in data


async def test_reminder_list_returns_pending_by_default(
    conn: AsyncConnection,
) -> None:
    set_tool = _by_name(build_productivity_tools(conn), "reminder_set")
    await set_tool.handler({"due_at": 1000, "message": "a"})
    await set_tool.handler({"due_at": 2000, "message": "b"})

    list_tool = _by_name(build_productivity_tools(conn), "reminder_list")
    data = json.loads(await list_tool.handler({}))
    assert [r["message"] for r in data] == ["a", "b"]


# ---------------------------------------------------------------------------
# todo_add / todo_list / todo_done
# ---------------------------------------------------------------------------
async def test_todo_add_with_list_tags(conn: AsyncConnection) -> None:
    tool = _by_name(build_productivity_tools(conn), "todo_add")
    data = json.loads(
        await tool.handler({"content": "fix bug", "tags": ["work", "urgent"]})
    )
    assert data["content"] == "fix bug"
    assert data["tags"] == "work,urgent"


async def test_todo_add_with_string_tag(conn: AsyncConnection) -> None:
    tool = _by_name(build_productivity_tools(conn), "todo_add")
    data = json.loads(await tool.handler({"content": "x", "tags": "personal"}))
    assert data["tags"] == "personal"


async def test_todo_list_returns_open_only_by_default(
    conn: AsyncConnection,
) -> None:
    add = _by_name(build_productivity_tools(conn), "todo_add")
    done = _by_name(build_productivity_tools(conn), "todo_done")
    listing = _by_name(build_productivity_tools(conn), "todo_list")

    a = json.loads(await add.handler({"content": "a"}))
    b = json.loads(await add.handler({"content": "b"}))
    await done.handler({"id": a["id"]})

    out = json.loads(await listing.handler({}))
    assert [t["id"] for t in out] == [b["id"]]


async def test_todo_list_filters_by_tag(conn: AsyncConnection) -> None:
    add = _by_name(build_productivity_tools(conn), "todo_add")
    listing = _by_name(build_productivity_tools(conn), "todo_list")

    work = json.loads(await add.handler({"content": "work item", "tags": ["work"]}))
    json.loads(await add.handler({"content": "home item", "tags": ["home"]}))

    out = json.loads(await listing.handler({"tag": "work"}))
    assert [t["id"] for t in out] == [work["id"]]


async def test_todo_done_marks_and_returns_payload(
    conn: AsyncConnection,
) -> None:
    add = _by_name(build_productivity_tools(conn), "todo_add")
    done = _by_name(build_productivity_tools(conn), "todo_done")

    t = json.loads(await add.handler({"content": "x"}))
    out = json.loads(await done.handler({"id": t["id"]}))
    assert out["id"] == t["id"]
    assert out["done_at"] is not None


async def test_todo_done_rejects_unknown_id(conn: AsyncConnection) -> None:
    done = _by_name(build_productivity_tools(conn), "todo_done")
    data = json.loads(await done.handler({"id": 99999}))
    assert "error" in data
