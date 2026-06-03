"""Tests for the /api/mcp/servers CRUD surface (Plan 32).

Drives the route through `LifespanManager` so the encryptor and the
fresh-DB fixture are wired the same way they are in production.

The lifespan installs a real `McpServerManager`, but tests swap its
`_connect` factory for a fake (built locally here) so we never actually
spawn an npx subprocess or open a streamable-HTTP session.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@dataclass
class _FakeTool:
    name: str
    description: str = ""
    inputSchema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


@dataclass
class _ListToolsReturn:
    tools: list[_FakeTool]


class _FakeSession:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = tools
        self.initialised = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> None:
        self.initialised = True

    async def list_tools(self) -> _ListToolsReturn:
        return _ListToolsReturn(tools=list(self._tools))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))

        class _R:
            content = []
            isError = False
            structuredContent = {"ok": True}

        return _R()


@asynccontextmanager
async def _fake_connect(server, secrets) -> AsyncIterator[_FakeSession]:
    """Connector swap: surface a stub session that exposes one tool per
    server. Per-server tool list keyed on the slug so tests can assert
    they got the right name back."""
    tools = [_FakeTool(name=f"tool_for_{server.name}")]
    session = _FakeSession(tools=tools)
    try:
        yield session
    finally:
        pass


@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        # Swap the manager's connector for the local fake.
        manager = app.state.mcp_servers_manager
        assert manager is not None
        original = manager._connect
        manager._connect = _fake_connect
        try:
            yield c
        finally:
            manager._connect = original
            await manager.stop_all()


# --- auth ------------------------------------------------------------------


async def test_list_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/mcp/servers")
    assert resp.status_code == 401


async def test_create_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/mcp/servers",
        json={
            "name": "fs",
            "display_name": "FS",
            "transport": "http",
            "url": "https://x",
        },
    )
    assert resp.status_code == 401


# --- create ----------------------------------------------------------------


async def test_create_http_server(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "http-mcp",
            "display_name": "HTTP MCP",
            "transport": "http",
            "url": "https://example.com/sse",
            "credentials": "bearer-xyz",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "http-mcp"
    assert body["transport"] == "http"
    assert body["url"] == "https://example.com/sse"
    # Critical secret-bereinigung:
    assert "bearer-xyz" not in resp.text
    assert "credentials" not in body
    assert "credentials_data" not in body
    assert "env_json" not in body
    assert body["enabled"] is True
    # Manager started it → status reports ready.
    assert body["status"] == "ready"


async def test_create_stdio_server_with_env(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "fs-stdio",
            "display_name": "Filesystem stdio",
            "transport": "stdio",
            "command_argv": ["npx", "-y", "@x/y", "/tmp"],
            "env": {"GITHUB_TOKEN": "ghp_secret", "DEBUG": "1"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # env_json never round-trips; env_keys gives the variable names only.
    assert "env_json" not in body
    assert "GITHUB_TOKEN" not in resp.text or "ghp_secret" not in resp.text
    assert "ghp_secret" not in resp.text
    assert sorted(body["env_keys"]) == ["DEBUG", "GITHUB_TOKEN"]
    assert body["transport"] == "stdio"
    assert body["command_argv"] == ["npx", "-y", "@x/y", "/tmp"]
    assert body["url"] is None


# --- list ------------------------------------------------------------------


async def test_list_returns_redacted_rows(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "secret-srv",
            "display_name": "Secret",
            "transport": "stdio",
            "command_argv": ["x"],
            "env": {"TOKEN": "very-secret"},
        },
    )
    resp = await client.get("/api/mcp/servers", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "very-secret" not in resp.text
    assert isinstance(body["servers"], list)
    by_name = {r["name"]: r for r in body["servers"]}
    row = by_name["secret-srv"]
    assert row["env_keys"] == ["TOKEN"]
    assert "credentials_data" not in row
    assert "env_json" not in row


# --- validation ------------------------------------------------------------


async def test_create_rejects_invalid_slug(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "Bad Slug",
            "display_name": "x",
            "transport": "http",
            "url": "https://x",
        },
    )
    assert resp.status_code in (400, 422)


async def test_create_rejects_missing_url_for_http(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "no-url",
            "display_name": "x",
            "transport": "http",
        },
    )
    assert resp.status_code in (400, 422)


async def test_create_rejects_missing_argv_for_stdio(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "no-argv",
            "display_name": "x",
            "transport": "stdio",
        },
    )
    assert resp.status_code in (400, 422)


async def test_create_409_on_duplicate(client: httpx.AsyncClient) -> None:
    body = {
        "name": "dup-srv",
        "display_name": "x",
        "transport": "http",
        "url": "https://x",
    }
    first = await client.post("/api/mcp/servers", headers=AUTH, json=body)
    assert first.status_code == 201
    second = await client.post("/api/mcp/servers", headers=AUTH, json=body)
    assert second.status_code == 409


# --- update ----------------------------------------------------------------


async def test_update_preserves_credentials_when_undefined(
    client: httpx.AsyncClient,
) -> None:
    create = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "keep-cred",
            "display_name": "Keep",
            "transport": "http",
            "url": "https://x",
            "credentials": "stay-here",
        },
    )
    server_id = create.json()["id"]
    # Update without `credentials` field → ciphertext stays intact.
    patch = await client.put(
        f"/api/mcp/servers/{server_id}",
        headers=AUTH,
        json={"display_name": "Keep v2"},
    )
    assert patch.status_code == 200
    assert patch.json()["display_name"] == "Keep v2"
    # The ciphertext is no longer accessible from the public API; we just
    # confirm the redaction stays in place and no plaintext leaks.
    assert "stay-here" not in patch.text


async def test_update_clears_credentials_with_explicit_null(
    client: httpx.AsyncClient,
) -> None:
    from hermes.main import app as _app
    from hermes.repository import mcp_servers as _repo

    create = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "clear-cred",
            "display_name": "Clear",
            "transport": "http",
            "url": "https://x",
            "credentials": "wipe-me",
        },
    )
    server_id = create.json()["id"]
    # Confirm the ciphertext landed before we ask for the clear, otherwise
    # the post-PUT assertion is trivially true (no creds to clear in the
    # first place).
    before = await _repo.get(_app.state.db, server_id)
    assert before is not None
    assert before.credentials_iv is not None

    patch = await client.put(
        f"/api/mcp/servers/{server_id}",
        headers=AUTH,
        json={"credentials": None},
    )
    assert patch.status_code == 200, patch.text

    # Sentinel boundary: `credentials: null` MUST clear the persisted
    # ciphertext tripel. The API response can't show this directly (the
    # ciphertext is redacted on read), so verify against the repo.
    after = await _repo.get(_app.state.db, server_id)
    assert after is not None
    assert after.credentials_iv is None
    assert after.credentials_tag is None
    assert after.credentials_data is None


async def test_update_keeps_credentials_when_field_omitted(
    client: httpx.AsyncClient,
) -> None:
    """Sentinel boundary: omitting `credentials` from a PUT must leave the
    stored ciphertext intact. Mirrors the typical edit-display-name flow
    where the user never types into the credentials field."""
    from hermes.main import app as _app
    from hermes.repository import mcp_servers as _repo

    create = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "omit-cred",
            "display_name": "Omit",
            "transport": "http",
            "url": "https://x",
            "credentials": "stays-put",
        },
    )
    server_id = create.json()["id"]
    before = await _repo.get(_app.state.db, server_id)
    assert before is not None
    iv_before = before.credentials_iv

    patch = await client.put(
        f"/api/mcp/servers/{server_id}",
        headers=AUTH,
        json={"display_name": "Omit v2"},
    )
    assert patch.status_code == 200

    after = await _repo.get(_app.state.db, server_id)
    assert after is not None
    assert after.credentials_iv == iv_before  # untouched


async def test_update_unknown_404(client: httpx.AsyncClient) -> None:
    patch = await client.put(
        "/api/mcp/servers/99999",
        headers=AUTH,
        json={"display_name": "ghost"},
    )
    assert patch.status_code == 404


# --- delete + restart ------------------------------------------------------


async def test_delete_removes_server(client: httpx.AsyncClient) -> None:
    create = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "to-del",
            "display_name": "del",
            "transport": "http",
            "url": "https://x",
        },
    )
    server_id = create.json()["id"]
    resp = await client.delete(f"/api/mcp/servers/{server_id}", headers=AUTH)
    assert resp.status_code == 204
    after = await client.get("/api/mcp/servers", headers=AUTH)
    names = {r["name"] for r in after.json()["servers"]}
    assert "to-del" not in names


async def test_delete_unknown_404(client: httpx.AsyncClient) -> None:
    resp = await client.delete("/api/mcp/servers/99999", headers=AUTH)
    assert resp.status_code == 404


async def test_restart_endpoint(client: httpx.AsyncClient) -> None:
    create = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "restart-me",
            "display_name": "r",
            "transport": "http",
            "url": "https://x",
        },
    )
    server_id = create.json()["id"]
    resp = await client.post(
        f"/api/mcp/servers/{server_id}/restart", headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in ("ready", "starting")


async def test_health_endpoint_per_server(client: httpx.AsyncClient) -> None:
    create = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "health-srv",
            "display_name": "h",
            "transport": "http",
            "url": "https://x",
        },
    )
    server_id = create.json()["id"]
    resp = await client.get(
        f"/api/mcp/servers/{server_id}/health", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    # The fake connector advertises exactly one tool per server.
    assert body["tool_count"] == 1
    assert body["last_error"] is None


# --- catalog interaction ---------------------------------------------------


async def test_create_makes_tool_visible_via_api_tools(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "visible-srv",
            "display_name": "v",
            "transport": "http",
            "url": "https://x",
        },
    )
    tools_resp = await client.get("/api/tools", headers=AUTH)
    assert tools_resp.status_code == 200
    body = tools_resp.json()
    sources = {t["source"] for t in body["tools"]}
    assert "mcp:visible-srv" in sources
    names = {t["name"] for t in body["tools"]}
    assert "visible-srv__tool_for_visible-srv" in names


async def test_delete_removes_tool_from_api_tools(
    client: httpx.AsyncClient,
) -> None:
    create = await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "fade-srv",
            "display_name": "f",
            "transport": "http",
            "url": "https://x",
        },
    )
    server_id = create.json()["id"]
    before = (await client.get("/api/tools", headers=AUTH)).json()
    assert any(t["source"] == "mcp:fade-srv" for t in before["tools"])
    await client.delete(f"/api/mcp/servers/{server_id}", headers=AUTH)
    after = (await client.get("/api/tools", headers=AUTH)).json()
    assert not any(t["source"] == "mcp:fade-srv" for t in after["tools"])


# --- /api/mcp/health refactor (aggregate) ----------------------------------


async def test_aggregate_mcp_health_lists_registered_servers(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/mcp/servers",
        headers=AUTH,
        json={
            "name": "agg-srv",
            "display_name": "agg",
            "transport": "http",
            "url": "https://x",
        },
    )
    resp = await client.get("/api/mcp/health", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    # The aggregate response still has the legacy `status` / `url` /
    # `tool_count` / `message` keys so the existing Plan 31 card keeps
    # working; Plan 32 only adds a new `servers` summary.
    assert body["status"] == "ok"
    assert "servers" in body
    by_name = {s["name"]: s for s in body["servers"]}
    assert "agg-srv" in by_name
    assert by_name["agg-srv"]["status"] == "ready"
