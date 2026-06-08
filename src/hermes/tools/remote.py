"""RemoteTool factory for the VS Code WebSocket agent (Plan 41).

Creates Tool instances whose handlers delegate execution to the connected
VS Code extension over the WebSocket. The extension runs the tool locally
(filesystem, terminal) and returns the result via a tool_result message.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from hermes.agent import Tool

if TYPE_CHECKING:
    from hermes.routes.ws_agent import WsSession

# Tool names that are safe to execute in plan mode (read-only operations).
PLAN_MODE_READ_ONLY: frozenset[str] = frozenset({"read_file", "list_dir", "get_selection"})


def make_remote_tool(name: str, session: WsSession) -> Tool:
    """Return a Tool whose handler forwards execution to the VS Code extension.

    Sends a tool_call message over the WebSocket and awaits the matching
    tool_result. Raises TimeoutError after 30 seconds if the client doesn't
    respond.
    """

    async def handler(params: dict[str, Any]) -> str:
        call_id = uuid4().hex
        await session.ws.send_json(
            {"type": "tool_call", "id": call_id, "name": name, "params": params}
        )
        return await session.wait_for_result(call_id, timeout=30.0)

    return Tool(
        name=name,
        description=f"Executes {name} in the connected VS Code extension.",
        parameters_schema={"type": "object", "additionalProperties": True},
        handler=handler,
    )
