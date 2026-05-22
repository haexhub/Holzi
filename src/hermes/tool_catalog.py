import httpx
from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.agent import Tool
from hermes.signal.client import SignalClient
from hermes.tools.cross_channel import build_cross_channel_tools
from hermes.tools.external import build_external_tools
from hermes.tools.memory import build_memory_tools
from hermes.tools.productivity import build_productivity_tools


def build_tool_catalog(
    *,
    db: AsyncConnection,
    signal_client: SignalClient | None,
    signal_self_number: str | None,
    external_http: httpx.AsyncClient | None,
    brave_api_key: str | None,
    current_channel: str | None = None,
) -> list[Tool]:
    """Assemble the full Hermes tool catalog.

    `current_channel` is forwarded to cross_channel_send so it can refuse to
    write back into the channel that produced the agent request (the
    recursion guard). For the MCP/manifest endpoints `current_channel` stays
    None — they're consumed by external callers (Cline, HaexChat, ...) that
    don't have a single "current channel" notion.
    """
    return (
        build_memory_tools(db)
        + build_cross_channel_tools(
            db,
            signal_client,
            signal_self_number,
            current_channel=current_channel,
        )
        + build_productivity_tools(db)
        + build_external_tools(external_http, brave_api_key)
    )
