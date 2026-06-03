"""GET /api/mcp/health — surface that the MCP streamable-HTTP mount is alive.

Reports whether the lifespan assembled the `StreamableHTTPSessionManager`
(stored on `app.state.mcp_manager`) plus how many tools the catalog
currently exposes. Consumed by the `/settings/skills` MCP card so external
client setup issues ("Cline can't connect") surface in the UI instead of
landing in a bug report.

State-only on purpose: a real HTTP probe against `/mcp` would have to
re-enter the ASGI stack and would require a full MCP session handshake
the Streamable-HTTP handler refuses to short-circuit. The state check is
the honest "mount point is wired and tools are loaded" answer.
"""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class McpHealthResponse(BaseModel):
    status: Literal["ok", "error"]
    url: str
    tool_count: int
    message: str


@router.get("/health", response_model=McpHealthResponse)
async def api_mcp_health(request: Request) -> McpHealthResponse:
    manager = getattr(request.app.state, "mcp_manager", None)
    if manager is None:
        return McpHealthResponse(
            status="error",
            url="/mcp",
            tool_count=0,
            message="MCP-Session-Manager nicht initialisiert",
        )
    catalog = list(request.app.state.tool_catalog or [])
    return McpHealthResponse(
        status="ok",
        url="/mcp",
        tool_count=len(catalog),
        message="bereit",
    )
