from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from hermes.agent import Tool

SERVER_NAME = "hermes"


def build_mcp_server(tools: list[Tool]) -> Server:
    """Construct a low-level MCP Server bound to the given Hermes tool catalog."""
    server: Server = Server(SERVER_NAME)
    lookup = {t.name: t for t in tools}

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.parameters_schema,
            )
            for t in tools
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        tool = lookup.get(name)
        if tool is None:
            return [types.TextContent(type="text", text=f"error: unknown tool {name!r}")]
        result = await tool.handler(arguments or {})
        return [types.TextContent(type="text", text=result)]

    return server


@asynccontextmanager
async def mcp_session_manager(
    tools: list[Tool],
) -> AsyncIterator[StreamableHTTPSessionManager]:
    """Lifespan helper that boots a streamable-HTTP MCP session manager."""
    server = build_mcp_server(tools)
    manager = StreamableHTTPSessionManager(
        server,
        stateless=True,
        json_response=True,
    )
    async with manager.run():
        yield manager


async def mcp_asgi_handler(
    manager: StreamableHTTPSessionManager,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    await manager.handle_request(scope, receive, send)


def tool_manifest(tools: list[Tool]) -> dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.parameters_schema,
            }
            for t in tools
        ],
    }
