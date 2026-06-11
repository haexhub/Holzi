"""Tests for `GET /api/tools` (Plan 31).

Read-only tool inventory backed by `app.state.tool_catalog`. Surfaces the
exact list MCP exposes — no second build path. The endpoint is consumed
by the `/settings/skills` page and (later) by Plan 29-E's persona tool
allowlist multi-select.
"""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.agent import Tool
from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
async def client(pg_db):
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


async def test_tools_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/tools")
    assert response.status_code == 401


async def test_tools_returns_alphabetical_catalog(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/tools", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert "tools" in body
    assert "total" in body
    assert body["total"] == len(body["tools"])
    names = [t["name"] for t in body["tools"]]
    assert names == sorted(names)


async def test_tools_each_entry_has_required_shape(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/tools", headers=AUTH)).json()
    assert body["tools"], "expected at least one built-in tool"
    for tool in body["tools"]:
        assert set(tool.keys()) >= {
            "name",
            "description",
            "requires_approval",
            "risk_reason",
            "parameters_schema",
            "source",
        }
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["description"], str)
        assert isinstance(tool["requires_approval"], bool)
        assert tool["risk_reason"] is None or isinstance(tool["risk_reason"], str)
        assert isinstance(tool["parameters_schema"], dict)
        assert tool["source"] == "builtin"


async def test_tools_contains_known_builtins(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/tools", headers=AUTH)).json()
    names = {t["name"] for t in body["tools"]}
    assert {"save_note", "web_search", "read_user_guide"} <= names


async def test_tools_propagates_requires_approval(
    client: httpx.AsyncClient,
) -> None:
    """Inject a synthetic tool with `requires_approval=True` into the live
    catalog so we can prove the flag and risk_reason are passed through.
    """
    risky = Tool(
        name="__test_risky_tool",
        description="synthetic risky tool",
        parameters_schema={"type": "object", "properties": {}},
        handler=lambda _args: (_ for _ in ()).throw(  # type: ignore[arg-type]
            AssertionError("handler should not run in this test")
        ),
        requires_approval=True,
        risk_reason="will eat your laundry",
    )
    catalog = list(app.state.tool_catalog)
    app.state.tool_catalog = [*catalog, risky]
    try:
        body = (await client.get("/api/tools", headers=AUTH)).json()
    finally:
        app.state.tool_catalog = catalog
    entry = next(
        t for t in body["tools"] if t["name"] == "__test_risky_tool"
    )
    assert entry["requires_approval"] is True
    assert entry["risk_reason"] == "will eat your laundry"
    assert entry["source"] == "builtin"
