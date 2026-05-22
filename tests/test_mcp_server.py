from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.agent import Tool
from hermes.main import app
from hermes.mcp_server import build_mcp_server, tool_manifest

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


async def _echo_handler(args: dict[str, Any]) -> str:
    return f"echoed: {args.get('text', '')}"


_ECHO_TOOL = Tool(
    name="echo",
    description="Echo back the input text",
    parameters_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    handler=_echo_handler,
)


async def test_build_mcp_server_returns_named_server() -> None:
    server = build_mcp_server([_ECHO_TOOL])
    assert server.name == "hermes"


def test_tool_manifest_serialises_catalog() -> None:
    manifest = tool_manifest([_ECHO_TOOL])
    assert manifest["name"] == "hermes"
    assert manifest["tools"] == [
        {
            "name": "echo",
            "description": "Echo back the input text",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    ]


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------
@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


async def test_mcp_manifest_returns_catalog(client: httpx.AsyncClient) -> None:
    response = await client.get("/mcp/manifest", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "hermes"
    names = {t["name"] for t in body["tools"]}
    # Phase 6 catalog
    assert {
        "recall_memory",
        "list_conversations",
        "get_conversation",
        "save_note",
        "get_note",
        "find_notes",
        "cross_channel_send",
    } <= names


async def test_mcp_manifest_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/mcp/manifest")
    assert response.status_code == 401


async def test_mcp_endpoint_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401


async def test_mcp_endpoint_lists_tools_via_jsonrpc(
    client: httpx.AsyncClient,
) -> None:
    init_response = await client.post(
        "/mcp/",
        headers={
            **AUTH,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0.0.0"},
            },
        },
    )
    assert init_response.status_code == 200
    session_id = init_response.headers.get("mcp-session-id")

    # Send the initialized notification.
    await client.post(
        "/mcp/",
        headers={
            **AUTH,
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id or "",
        },
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )

    list_response = await client.post(
        "/mcp/",
        headers={
            **AUTH,
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id or "",
        },
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert list_response.status_code == 200
    body = list_response.json()
    tool_names = {t["name"] for t in body["result"]["tools"]}
    assert "recall_memory" in tool_names
    assert "cross_channel_send" in tool_names
