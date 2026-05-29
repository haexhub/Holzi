"""Agent-facing tools for managing `agent_tasks` (Plan 16).

Replaces the old `reminder_*` / `todo_*` tools. A task is either one-shot
(`due_at` set) or recurring (`schedule` set). The agent decides which to
use when the user asks "remind me tomorrow at 9" (one-shot) vs "send me
a weekly summary every Monday morning" (recurring).
"""
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.repository import agent_tasks


def build_productivity_tools(db: AsyncEngine) -> list[Tool]:
    return [
        _task_create(db),
        _task_list(db),
        _task_delete(db),
    ]


def _task_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "prompt": t.prompt,
        "due_at": t.due_at,
        "schedule": t.schedule,
        "timezone": t.timezone,
        "enabled": t.enabled,
        "last_run_at": t.last_run_at,
        "last_status": t.last_status,
    }


def _task_create(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        title = str(args.get("title", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        if not title or not prompt:
            return json.dumps({"error": "title and prompt are required"})

        due_at_arg = args.get("due_at")
        schedule_arg = args.get("schedule")
        if (due_at_arg is None) == (schedule_arg is None):
            return json.dumps(
                {"error": "exactly one of due_at / schedule must be set"}
            )

        due_at: int | None = None
        if due_at_arg is not None:
            try:
                due_at = int(due_at_arg)
            except (TypeError, ValueError):
                return json.dumps({"error": "due_at must be an integer unix timestamp"})

        schedule: str | None = None
        if schedule_arg is not None:
            if not isinstance(schedule_arg, str):
                return json.dumps({"error": "schedule must be a string"})
            schedule = schedule_arg
            try:
                agent_tasks.validate_schedule(schedule)
            except ValueError as exc:
                return json.dumps({"error": str(exc)})

        timezone = str(args.get("timezone", "UTC"))

        t = await agent_tasks.create(
            db,
            title=title,
            prompt=prompt,
            due_at=due_at,
            schedule=schedule,
            timezone=timezone,
        )
        return json.dumps(_task_to_dict(t))

    return Tool(
        name="task_create",
        description=(
            "Create a scheduled or one-shot agent task. Exactly one of "
            "`due_at` (unix epoch seconds, one-shot) or `schedule` "
            "(5-field cron string, recurring) must be set. The agent is "
            "expected to resolve natural-language times (e.g. 'tomorrow "
            "9am') before calling. `prompt` is what the agent will execute "
            "when the task fires."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "prompt": {"type": "string"},
                "due_at": {
                    "type": "integer",
                    "description": "Unix epoch seconds. Mutually exclusive with schedule.",
                },
                "schedule": {
                    "type": "string",
                    "description": "5-field cron expression. Mutually exclusive with due_at.",
                },
                "timezone": {"type": "string", "default": "UTC"},
            },
            "required": ["title", "prompt"],
        },
        handler=handler,
    )


def _task_list(db: AsyncEngine) -> Tool:
    async def handler(_args: dict[str, Any]) -> str:
        items = await agent_tasks.list_all(db)
        return json.dumps([_task_to_dict(t) for t in items])

    return Tool(
        name="task_list",
        description=(
            "List all agent tasks (enabled and disabled). Returns nearest-"
            "due first. Each item carries last_run_at + last_status so the "
            "agent can decide whether to retry / report on a stuck task."
        ),
        parameters_schema={"type": "object", "properties": {}},
        handler=handler,
    )


def _task_delete(db: AsyncEngine) -> Tool:
    async def handler(args: dict[str, Any]) -> str:
        try:
            task_id = int(args["id"])
        except (KeyError, TypeError, ValueError):
            return json.dumps({"error": "id (integer) is required"})
        ok = await agent_tasks.delete(db, task_id)
        if not ok:
            return json.dumps({"error": f"task {task_id} not found"})
        return json.dumps({"id": task_id, "deleted": True})

    return Tool(
        name="task_delete",
        description="Delete an agent task by id. Returns {deleted: true} on success.",
        parameters_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        handler=handler,
    )
