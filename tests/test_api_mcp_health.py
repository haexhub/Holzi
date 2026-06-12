"""Tests for `GET /api/mcp/health` (Plan 31).

State-only check — no HTTP self-probe of the `/mcp` mount. Surfaces
whether the MCP session manager was assembled by the lifespan and how
many tools the catalog currently exposes. The `/settings/skills` page
renders this as a Health card; Plan 32 will later return a per-server
list when external MCP servers can be registered.
"""
import httpx

from hermes.agent import Tool
from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}




async def test_mcp_health_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/mcp/health")
    assert response.status_code == 401


async def test_mcp_health_ok_when_manager_initialised(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/mcp/health", headers=AUTH)).json()
    assert body["status"] == "ok"
    assert body["url"] == "/mcp"
    assert body["tool_count"] == len(app.state.tool_catalog)
    assert isinstance(body["message"], str) and body["message"]


async def test_mcp_health_error_when_manager_missing(
    client: httpx.AsyncClient,
) -> None:
    original = app.state.mcp_manager
    app.state.mcp_manager = None
    try:
        body = (await client.get("/api/mcp/health", headers=AUTH)).json()
    finally:
        app.state.mcp_manager = original
    assert body["status"] == "error"
    assert "nicht initialisiert" in body["message"]
    assert body["url"] == "/mcp"


async def test_mcp_health_reflects_catalog_size(
    client: httpx.AsyncClient,
) -> None:
    extra = Tool(
        name="__test_extra_tool",
        description="synthetic extra tool for health-count assertion",
        parameters_schema={"type": "object", "properties": {}},
        handler=lambda _args: (_ for _ in ()).throw(  # type: ignore[arg-type]
            AssertionError("handler should not run in this test")
        ),
    )
    catalog = list(app.state.tool_catalog)
    app.state.tool_catalog = [*catalog, extra]
    try:
        body = (await client.get("/api/mcp/health", headers=AUTH)).json()
    finally:
        app.state.tool_catalog = catalog
    assert body["tool_count"] == len(catalog) + 1
    assert body["status"] == "ok"
