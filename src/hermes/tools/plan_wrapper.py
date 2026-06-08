"""PlanOnlyWrapper factory for VS Code plan mode (Plan 41).

In plan mode, write and run tools are replaced by wrappers that return a
human-readable description of what would have been executed. The agent then
phrases its response as "I would do X" rather than actually running the tool.
"""
from typing import Any

from hermes.agent import Tool

# Re-exported so callers can do: from hermes.tools.plan_wrapper import PLAN_MODE_READ_ONLY
from hermes.tools.remote import PLAN_MODE_READ_ONLY

__all__ = ["PLAN_MODE_READ_ONLY", "make_plan_wrapper"]


def make_plan_wrapper(name: str) -> Tool:
    """Return a Tool that describes its execution instead of performing it.

    Used for write and run tools when permission_mode is 'plan'.
    """

    async def handler(params: dict[str, Any]) -> str:
        args_str = ", ".join(f"{k}={v!r}" for k, v in sorted(params.items()))
        return f"[plan mode] would call {name}({args_str})"

    return Tool(
        name=name,
        description=f"Describes what {name} would do (plan mode — not executed).",
        parameters_schema={"type": "object", "additionalProperties": True},
        handler=handler,
    )
