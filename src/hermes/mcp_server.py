from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from hermes.agent import Tool

SERVER_NAME = "hermes"

# Provider returning the *current* tool catalog. The inbound /mcp server is
# mounted once for the process lifetime but the catalog changes at runtime
# (MCP servers installed via the UI or the `mcp_install` meta-tool), so the
# handlers read through this callable rather than snapshotting at mount time —
# otherwise `/mcp` would serve a stale tool set until restart.
ToolProvider = Callable[[], list[Tool]]


def _unique_lookup(tools: list[Tool]) -> dict[str, Tool]:
    lookup: dict[str, Tool] = {}
    for t in tools:
        if t.name in lookup:
            raise ValueError(f"duplicate tool name in MCP catalog: {t.name!r}")
        lookup[t.name] = t
    return lookup


def build_mcp_server(tools_provider: ToolProvider) -> Server:
    """Construct a low-level MCP Server bound to the live Hermes tool catalog.

    `tools_provider` is read on every `list_tools` / `call_tool` so a server
    installed at runtime shows up without remounting. Uniqueness is validated
    once at build (catches a programming error early); the per-call lookup
    tolerates the catalog growing afterwards.
    """
    server: Server = Server(SERVER_NAME)
    _unique_lookup(tools_provider())  # fail fast on a duplicate at boot

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.parameters_schema,
            )
            for t in tools_provider()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        tool = {t.name: t for t in tools_provider()}.get(name)
        if tool is None:
            return [types.TextContent(type="text", text=f"error: unknown tool {name!r}")]
        if arguments is None:
            safe_args: dict[str, Any] = {}
        elif not isinstance(arguments, dict):
            return [
                types.TextContent(
                    type="text",
                    text=f"error: arguments for tool {name!r} must be a JSON object",
                )
            ]
        else:
            safe_args = arguments
        result = await tool.handler(safe_args)
        return [types.TextContent(type="text", text=result)]

    return server


@asynccontextmanager
async def mcp_session_manager(
    tools_provider: ToolProvider,
) -> AsyncIterator[StreamableHTTPSessionManager]:
    """Lifespan helper that boots a streamable-HTTP MCP session manager bound
    to the live catalog via `tools_provider`."""
    server = build_mcp_server(tools_provider)
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
