"""GET /api/tools — read-only inventory of the agent's tool catalog (Plan 31).

Surfaces what `app.state.tool_catalog` (assembled once in the lifespan with
`current_channel=None`) currently exposes. The `/settings/skills` page
renders this as a flat alphabetical list with `parameters_schema` viewers
and approval badges; Plan 29-E will reuse it as the data source for the
persona tool-allowlist multi-select.

The endpoint never builds the catalog itself — it reads the same list MCP
exposes, so the two surfaces can never drift. Sorting and the `source`
projection happen at request time so Plan 32 can drop MCP-sourced tools
into the catalog with their own `source="mcp:<server-name>"` and have them
appear in the response without further changes here.
"""
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from hermes.agent import Tool

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolInfo(BaseModel):
    name: str
    description: str
    requires_approval: bool
    risk_reason: str | None
    parameters_schema: dict[str, Any]
    # Free-form so Plan 32 can extend with `"mcp:<server-name>"` without
    # breaking the OpenAPI schema contract this endpoint commits to today.
    source: str


class ToolsResponse(BaseModel):
    tools: list[ToolInfo]
    total: int


def _to_info(t: Tool) -> ToolInfo:
    return ToolInfo(
        name=t.name,
        description=t.description,
        requires_approval=t.requires_approval,
        risk_reason=t.risk_reason,
        parameters_schema=t.parameters_schema,
        source="builtin",
    )


@router.get("", response_model=ToolsResponse)
async def api_list_tools(request: Request) -> ToolsResponse:
    catalog: list[Tool] = list(request.app.state.tool_catalog or [])
    tools = sorted((_to_info(t) for t in catalog), key=lambda t: t.name)
    return ToolsResponse(tools=tools, total=len(tools))
