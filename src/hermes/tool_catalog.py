from typing import TYPE_CHECKING

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.signal.client import SignalClient
from hermes.tools.cross_channel import build_cross_channel_tools
from hermes.tools.external import build_external_tools
from hermes.tools.memory import build_memory_tools
from hermes.tools.productivity import build_productivity_tools
from hermes.tools.user_guide import build_user_guide_tools

if TYPE_CHECKING:  # pragma: no cover
    from hermes.mcp_manager import McpServerManager


def build_tool_catalog(
    *,
    db: AsyncEngine,
    signal_client: SignalClient | None,
    signal_self_number: str | None,
    external_http: httpx.AsyncClient | None,
    brave_api_key: str | None,
    mcp_manager: "McpServerManager | None" = None,
    current_channel: str | None = None,
) -> list[Tool]:
    """Assemble the full Hermes tool catalog.

    `current_channel` is forwarded to cross_channel_send so it can refuse to
    write back into the channel that produced the agent request (the
    recursion guard). For the MCP/manifest endpoints `current_channel` stays
    None — they're consumed by external callers (Cline, HaexChat, ...) that
    don't have a single "current channel" notion.

    `mcp_manager` is the Plan-32 external-MCP-server lifecycle manager.
    Its `aggregate_tools()` output is appended after the built-ins; tools
    keep their `source="mcp:<server-name>"` markers. None disables the
    merge (used by tests and by the catalog snapshot built before the
    manager itself is constructed during lifespan).
    """
    builtin: list[Tool] = (
        build_memory_tools(db)
        + build_cross_channel_tools(
            db,
            signal_client,
            signal_self_number,
            current_channel=current_channel,
        )
        + build_productivity_tools(db)
        + build_external_tools(external_http, brave_api_key)
        + build_user_guide_tools()
    )
    # `Tool.source` defaults to "builtin" on the dataclass — every builder
    # leaves it at the default, so the merge needs no explicit annotation
    # pass here. If a future builder ever sets a different source on its
    # tools, this comment is the place to revisit.
    if mcp_manager is None:
        return builtin
    return builtin + mcp_manager.aggregate_tools()
