import json

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.repository import agent_tasks
from hermes.tools.productivity import build_productivity_tools


def _by_name(tools: list[Tool], name: str) -> Tool:
    return next(t for t in tools if t.name == name)


async def test_productivity_tools_surface(conn: AsyncEngine) -> None:
    names = {t.name for t in build_productivity_tools(conn)}
    assert names == {"task_create", "task_list", "task_delete"}


# ---------------------------------------------------------------------------
# task_create
# ---------------------------------------------------------------------------


async def test_task_create_one_shot(conn: AsyncEngine) -> None:
    tool = _by_name(build_productivity_tools(conn), "task_create")
    raw = await tool.handler({
        "title": "ping",
        "prompt": "say hi",
        "due_at": 2_000_000_000,
    })
    out = json.loads(raw)
    assert out["title"] == "ping"
    assert out["due_at"] == 2_000_000_000
    assert out["schedule"] is None


async def test_task_create_recurring(conn: AsyncEngine) -> None:
    tool = _by_name(build_productivity_tools(conn), "task_create")
    raw = await tool.handler({
        "title": "daily",
        "prompt": "summary",
        "schedule": "0 8 * * *",
    })
    out = json.loads(raw)
    assert out["schedule"] == "0 8 * * *"
    assert out["due_at"] is not None


async def test_task_create_rejects_missing_title(conn: AsyncEngine) -> None:
    tool = _by_name(build_productivity_tools(conn), "task_create")
    raw = await tool.handler({"prompt": "x", "due_at": 1})
    assert json.loads(raw)["error"]


async def test_task_create_rejects_non_string_title(conn: AsyncEngine) -> None:
    # Guard against silent str() coercion of None/booleans/ints — would
    # otherwise turn `title=None` into the literal string `"None"`.
    tool = _by_name(build_productivity_tools(conn), "task_create")
    raw = await tool.handler({"title": 42, "prompt": "x", "due_at": 1})
    assert "strings" in json.loads(raw)["error"]


async def test_task_create_rejects_unknown_timezone(conn: AsyncEngine) -> None:
    tool = _by_name(build_productivity_tools(conn), "task_create")
    raw = await tool.handler({
        "title": "t",
        "prompt": "x",
        "schedule": "0 8 * * *",
        "timezone": "Mars/Olympus_Mons",
    })
    out = json.loads(raw)
    assert "error" in out


async def test_task_create_rejects_both_due_at_and_schedule(
    conn: AsyncEngine,
) -> None:
    tool = _by_name(build_productivity_tools(conn), "task_create")
    raw = await tool.handler({
        "title": "x",
        "prompt": "y",
        "due_at": 1,
        "schedule": "0 8 * * *",
    })
    assert "exactly one" in json.loads(raw)["error"]


async def test_task_create_rejects_invalid_cron(conn: AsyncEngine) -> None:
    tool = _by_name(build_productivity_tools(conn), "task_create")
    raw = await tool.handler({
        "title": "x",
        "prompt": "y",
        "schedule": "garbage",
    })
    assert "invalid cron" in json.loads(raw)["error"]


# ---------------------------------------------------------------------------
# task_list / task_delete
# ---------------------------------------------------------------------------


async def test_task_list_returns_all(conn: AsyncEngine) -> None:
    await agent_tasks.create(conn, user_id=1, title="a", prompt="x", due_at=1)
    await agent_tasks.create(conn, user_id=1, title="b", prompt="x", due_at=2)
    tool = _by_name(build_productivity_tools(conn), "task_list")
    out = json.loads(await tool.handler({}))
    assert len(out) == 2
    assert {t["title"] for t in out} == {"a", "b"}


async def test_task_delete_removes(conn: AsyncEngine) -> None:
    t = await agent_tasks.create(conn, user_id=1, title="x", prompt="x", due_at=1)
    tool = _by_name(build_productivity_tools(conn), "task_delete")
    raw = await tool.handler({"id": t.id})
    out = json.loads(raw)
    assert out["deleted"] is True
    assert await agent_tasks.get(conn, t.id, user_id=1) is None


async def test_task_delete_unknown_returns_error(conn: AsyncEngine) -> None:
    tool = _by_name(build_productivity_tools(conn), "task_delete")
    out = json.loads(await tool.handler({"id": 9999}))
    assert "not found" in out["error"]
