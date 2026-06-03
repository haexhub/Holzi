"""GET /api/mcp/health — aggregate MCP health (Plan 31 + Plan 32).

Plan 31 shipped this as a state-only "is the StreamableHTTP session
manager wired up?" probe. Plan 32 extends the response with a `servers`
list: every registered external MCP server's lifecycle status. The
legacy keys (`status`, `url`, `tool_count`, `message`) stay so the
existing Plan-31 card keeps rendering without changes.

The /mcp mount itself (our outbound StreamableHTTP server, where Cline /
HaexChat connect) still uses `app.state.mcp_manager`. The new outbound
client lifecycle for external MCP servers lives on
`app.state.mcp_servers_manager`. Distinct names because the surfaces are
unrelated despite sharing the "mcp" namespace.
"""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from hermes.mcp_manager import McpServerManager

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


StatusLiteral = Literal["starting", "ready", "crashed", "disabled", "unknown"]


class McpServerSummary(BaseModel):
    id: int
    name: str
    status: StatusLiteral
    last_error: str | None = None
    tool_count: int


class McpHealthResponse(BaseModel):
    status: Literal["ok", "error"]
    url: str
    tool_count: int
    message: str
    servers: list[McpServerSummary] = []


@router.get("/health", response_model=McpHealthResponse)
async def api_mcp_health(request: Request) -> McpHealthResponse:
    manager = getattr(request.app.state, "mcp_manager", None)
    servers_manager: McpServerManager | None = getattr(
        request.app.state, "mcp_servers_manager", None
    )

    if manager is None:
        # The StreamableHTTP mount itself isn't wired up — degraded mode.
        return McpHealthResponse(
            status="error",
            url="/mcp",
            tool_count=0,
            message="MCP-Session-Manager nicht initialisiert",
            servers=_summarise(servers_manager),
        )

    catalog = list(request.app.state.tool_catalog or [])
    return McpHealthResponse(
        status="ok",
        url="/mcp",
        tool_count=len(catalog),
        message="bereit",
        servers=_summarise(servers_manager),
    )


def _summarise(manager: McpServerManager | None) -> list[McpServerSummary]:
    if manager is None:
        return []
    out: list[McpServerSummary] = []
    for handle in manager.list_handles():
        out.append(
            McpServerSummary(
                id=handle.id,
                name=handle.name,
                status=handle.status,
                last_error=handle.last_error,
                tool_count=len(handle.tools),
            )
        )
    return out
