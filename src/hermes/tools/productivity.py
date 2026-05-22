import json
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.agent import Tool
from hermes.repository import reminders, todos


def build_productivity_tools(db: AsyncConnection) -> list[Tool]:
    return [
        _reminder_set(db),
        _reminder_list(db),
        _todo_add(db),
        _todo_list(db),
        _todo_done(db),
    ]


# ---------------------------------------------------------------------------
def _reminder_set(db: AsyncConnection) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        message = str(args["message"])
        channel = str(args.get("channel", "signal"))

        when_arg = args.get("due_at")
        if when_arg is None:
            return json.dumps({"error": "due_at (unix seconds) is required"})
        try:
            due_at = int(when_arg)
        except (TypeError, ValueError):
            return json.dumps({"error": "due_at must be an integer unix timestamp"})

        r = await reminders.create(db, due_at=due_at, message=message, channel=channel)
        return json.dumps(
            {
                "id": r.id,
                "due_at": r.due_at,
                "message": r.message,
                "channel": r.channel,
            }
        )

    return Tool(
        name="reminder_set",
        description=(
            "Schedule a reminder. The scheduler delivers it to the configured "
            "channel (default 'signal') once `due_at` is reached. `due_at` is a "
            "unix timestamp in seconds — the agent is expected to resolve "
            "natural-language times (e.g. 'tomorrow 9am') before calling."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "due_at": {"type": "integer", "description": "Unix epoch seconds."},
                "message": {"type": "string"},
                "channel": {"type": "string", "enum": ["signal"], "default": "signal"},
            },
            "required": ["due_at", "message"],
        },
        handler=handler,
    )


def _reminder_list(db: AsyncConnection) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        include_fired = bool(args.get("include_fired", False))
        rs = await reminders.list_all(db, include_fired=include_fired)
        return json.dumps(
            [
                {
                    "id": r.id,
                    "due_at": r.due_at,
                    "message": r.message,
                    "channel": r.channel,
                    "fired_at": r.fired_at,
                }
                for r in rs
            ]
        )

    return Tool(
        name="reminder_list",
        description="List pending reminders (or all if `include_fired=true`).",
        parameters_schema={
            "type": "object",
            "properties": {
                "include_fired": {"type": "boolean", "default": False},
            },
        },
        handler=handler,
    )


# ---------------------------------------------------------------------------
def _todo_add(db: AsyncConnection) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        content = str(args["content"])
        tags_arg = args.get("tags")
        if tags_arg is None or tags_arg == "":
            tags: str | None = None
        elif isinstance(tags_arg, str):
            tags = tags_arg
        elif isinstance(tags_arg, list):
            tags = ",".join(str(t) for t in tags_arg if str(t).strip())
        else:
            return json.dumps({"error": "tags must be a string or array of strings"})

        t = await todos.add(db, content=content, tags=tags or None)
        return json.dumps(
            {"id": t.id, "content": t.content, "tags": t.tags, "created_at": t.created_at}
        )

    return Tool(
        name="todo_add",
        description="Add a new todo item.",
        parameters_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["content"],
        },
        handler=handler,
    )


def _todo_list(db: AsyncConnection) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        only_open = bool(args.get("only_open", True))
        tag = args.get("tag")
        if tag is not None and not isinstance(tag, str):
            return json.dumps({"error": "tag must be a string"})

        items = await todos.list_all(db, only_open=only_open, tag=tag)
        return json.dumps(
            [
                {
                    "id": t.id,
                    "content": t.content,
                    "tags": t.tags,
                    "done_at": t.done_at,
                    "created_at": t.created_at,
                }
                for t in items
            ]
        )

    return Tool(
        name="todo_list",
        description=(
            "List todos. By default returns only open items; pass "
            "`only_open=false` to include completed ones, and `tag` to filter "
            "to items carrying that exact tag token."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "only_open": {"type": "boolean", "default": True},
                "tag": {"type": "string"},
            },
        },
        handler=handler,
    )


def _todo_done(db: AsyncConnection) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        try:
            todo_id = int(args["id"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({"error": "id (integer) is required"})

        marked = await todos.mark_done(db, todo_id, ts=int(time.time()))
        if not marked:
            return json.dumps({"error": f"todo {todo_id} not found or already done"})

        t = await todos.get(db, todo_id)
        if t is None:
            return json.dumps({"error": f"todo {todo_id} disappeared after mark_done"})
        return json.dumps(
            {"id": t.id, "content": t.content, "done_at": t.done_at}
        )

    return Tool(
        name="todo_done",
        description="Mark a todo item as completed.",
        parameters_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        handler=handler,
    )
