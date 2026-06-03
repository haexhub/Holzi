"""Unit tests for the Plan 32-A meta-tools (`tools/meta.py`).

`list_tools` is exercised against a crafted catalog provider; the MCP
tools (`mcp_status` / `mcp_install` / `mcp_restart`) run against a *real*
`McpServerManager` wired to an injectable fake connector (the same seam
`test_mcp_manager.py` uses) plus the per-test SQLite engine, so the
create → start → ready path is the production one without spawning a
subprocess.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from hermes.agent import Tool
from hermes.crypto import Encryptor
from hermes.mcp_manager import McpServerManager
from hermes.repository import mcp_servers as mcp_repo
from hermes.tools.meta import build_meta_tools, redact_mcp_install_params

# --- fakes (shaped just enough for the manager handshake) ------------------


@dataclass
class _FakeTool:
    name: str
    description: str = "fake mcp tool"
    inputSchema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


@dataclass
class _ListToolsReturn:
    tools: list[_FakeTool]


class _FakeSession:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = tools

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _ListToolsReturn:
        return _ListToolsReturn(tools=list(self._tools))


class _FakeConnector:
    """Per-server-name behaviour for the manager's `connect` seam."""

    def __init__(self) -> None:
        self.tools_by_name: dict[str, list[_FakeTool]] = {}
        self.raise_on_open: dict[str, Exception] = {}

    def configure(self, name: str, tool_names: list[str]) -> None:
        self.tools_by_name[name] = [_FakeTool(name=n) for n in tool_names]

    @asynccontextmanager
    async def __call__(self, server: Any, secrets: Any) -> AsyncIterator[_FakeSession]:
        if server.name in self.raise_on_open:
            raise self.raise_on_open[server.name]
        yield _FakeSession(self.tools_by_name.get(server.name, []))


def _manager(conn) -> tuple[McpServerManager, _FakeConnector]:
    connector = _FakeConnector()
    manager = McpServerManager(
        conn, encryptor=Encryptor(b"\x00" * 32), connect=connector
    )
    return manager, connector


def _meta_tools(
    conn,
    *,
    manager: McpServerManager | None = None,
    provider=lambda: [],
) -> dict[str, Tool]:
    tools = build_meta_tools(
        db=conn,
        mcp_manager=manager,
        encryptor=Encryptor(b"\x00" * 32),
        tool_catalog_provider=provider,
    )
    return {t.name: t for t in tools}


# --- redact_mcp_install_params ---------------------------------------------


def test_redact_masks_credentials_and_env_values() -> None:
    redacted = redact_mcp_install_params(
        {
            "name": "fs",
            "transport": "stdio",
            "command_argv": ["npx", "server"],
            "credentials": "bearer-xyz",
            "env": {"GITHUB_TOKEN": "ghp_secret", "HOME": "/tmp"},
        }
    )
    assert redacted["credentials"] == "[redacted, 10 chars]"
    # Keys stay visible, values masked.
    assert redacted["env"] == {"GITHUB_TOKEN": "[redacted]", "HOME": "[redacted]"}
    # Non-secret fields untouched.
    assert redacted["name"] == "fs"
    assert redacted["command_argv"] == ["npx", "server"]


def test_redact_is_a_copy_and_tolerates_missing_secrets() -> None:
    params = {"name": "fs", "transport": "http", "url": "http://x"}
    redacted = redact_mcp_install_params(params)
    assert redacted == params
    assert redacted is not params  # never mutates the caller's dict


# --- list_tools ------------------------------------------------------------


async def test_list_tools_returns_all_without_filter(conn) -> None:
    async def _h(args: dict[str, Any]) -> str:
        return "ok"

    catalog = [
        Tool(name="recall_memory", description="d", parameters_schema={}, handler=_h),
        Tool(
            name="filesystem__read_file",
            description="d",
            parameters_schema={},
            handler=_h,
            source="mcp:filesystem",
        ),
        Tool(
            name="mcp_install",
            description="d",
            parameters_schema={},
            handler=_h,
            requires_approval=True,
        ),
    ]
    tool = _meta_tools(conn, provider=lambda: catalog)["list_tools"]
    out = json.loads(await tool.handler({}))
    names = [t["name"] for t in out["tools"]]
    assert names == ["recall_memory", "filesystem__read_file", "mcp_install"]
    install = next(t for t in out["tools"] if t["name"] == "mcp_install")
    assert install["requires_approval"] is True
    assert install["source"] == "builtin"


async def test_list_tools_filters_by_source(conn) -> None:
    async def _h(args: dict[str, Any]) -> str:
        return "ok"

    catalog = [
        Tool(name="recall_memory", description="d", parameters_schema={}, handler=_h),
        Tool(
            name="filesystem__read_file",
            description="d",
            parameters_schema={},
            handler=_h,
            source="mcp:filesystem",
        ),
    ]
    tool = _meta_tools(conn, provider=lambda: catalog)["list_tools"]

    builtin = json.loads(await tool.handler({"source_filter": "builtin"}))
    assert [t["name"] for t in builtin["tools"]] == ["recall_memory"]

    mcp = json.loads(await tool.handler({"source_filter": "mcp:filesystem"}))
    assert [t["name"] for t in mcp["tools"]] == ["filesystem__read_file"]

    none = json.loads(await tool.handler({"source_filter": "mcp:nope"}))
    assert none["tools"] == []


def test_list_tools_does_not_require_approval(conn) -> None:
    tool = _meta_tools(conn)["list_tools"]
    assert tool.requires_approval is False
    assert tool.source == "builtin"


# --- mcp_status ------------------------------------------------------------


async def test_mcp_status_empty(conn) -> None:
    manager, _ = _manager(conn)
    tool = _meta_tools(conn, manager=manager)["mcp_status"]
    out = json.loads(await tool.handler({}))
    assert out == {"servers": []}


async def test_mcp_status_reports_ready_and_disabled(conn) -> None:
    manager, connector = _manager(conn)
    connector.configure("alpha", ["read", "write"])
    # alpha: enabled + started → ready with 2 tools.
    alpha = await mcp_repo.create(
        conn, name="alpha", display_name="Alpha", transport="stdio",
        command_argv=["npx", "alpha"],
    )
    await manager.start_server(alpha.id)
    # beta: disabled, never started → no handle → reported disabled.
    await mcp_repo.create(
        conn, name="beta", display_name="Beta", transport="http",
        url="http://beta", enabled=False,
    )

    tool = _meta_tools(conn, manager=manager)["mcp_status"]
    out = json.loads(await tool.handler({}))
    by_name = {s["name"]: s for s in out["servers"]}
    assert by_name["alpha"]["status"] == "ready"
    assert by_name["alpha"]["tool_count"] == 2
    assert by_name["beta"]["status"] == "disabled"
    assert by_name["beta"]["tool_count"] == 0


async def test_mcp_status_does_not_require_approval(conn) -> None:
    manager, _ = _manager(conn)
    assert _meta_tools(conn, manager=manager)["mcp_status"].requires_approval is False


# --- mcp_install -----------------------------------------------------------


async def test_mcp_install_requires_approval_with_risk_reason(conn) -> None:
    manager, _ = _manager(conn)
    tool = _meta_tools(conn, manager=manager)["mcp_install"]
    assert tool.requires_approval is True
    assert tool.risk_reason
    assert tool.redact_arguments is redact_mcp_install_params


async def test_mcp_install_happy_path_starts_and_lists_tools(conn) -> None:
    manager, connector = _manager(conn)
    connector.configure("filesystem", ["read_file", "write_file"])
    tool = _meta_tools(conn, manager=manager)["mcp_install"]

    out = json.loads(
        await tool.handler(
            {
                "name": "filesystem",
                "transport": "stdio",
                "command_argv": ["npx", "@modelcontextprotocol/server-filesystem", "/tmp"],
            }
        )
    )
    assert out["success"] is True
    assert out["server"]["name"] == "filesystem"
    assert out["server"]["status"] == "ready"
    # Tool names are namespaced by the manager's wrapper.
    assert set(out["tools_added"]) == {"filesystem__read_file", "filesystem__write_file"}
    # Row persisted + aggregate_tools sees them.
    assert await mcp_repo.get_by_name(conn, "filesystem") is not None
    assert len(manager.aggregate_tools()) == 2


async def test_mcp_install_persists_credentials_encrypted(conn) -> None:
    manager, connector = _manager(conn)
    connector.configure("github", ["search"])
    tool = _meta_tools(conn, manager=manager)["mcp_install"]

    out = json.loads(
        await tool.handler(
            {
                "name": "github",
                "transport": "http",
                "url": "https://api.example/mcp",
                "credentials": "ghp_secret_token",
            }
        )
    )
    assert out["success"] is True
    # Plaintext never appears in the public summary.
    assert "ghp_secret_token" not in json.dumps(out)
    # But it round-trips through the decrypt path.
    row = await mcp_repo.get_by_name(conn, "github")
    secrets = await mcp_repo.read_secrets(
        conn, row.id, encryptor=Encryptor(b"\x00" * 32)
    )
    assert secrets.credentials == "ghp_secret_token"


async def test_mcp_install_rejects_bad_slug(conn) -> None:
    manager, _ = _manager(conn)
    tool = _meta_tools(conn, manager=manager)["mcp_install"]
    out = json.loads(
        await tool.handler({"name": "Bad Name!", "transport": "stdio", "command_argv": ["x"]})
    )
    assert out["success"] is False
    assert "kebab" in out["error"].lower() or "slug" in out["error"].lower()
    # Nothing persisted.
    assert await mcp_repo.list_all(conn) == []


async def test_mcp_install_rejects_transport_mismatch(conn) -> None:
    manager, _ = _manager(conn)
    tool = _meta_tools(conn, manager=manager)["mcp_install"]
    # http without url
    http_no_url = json.loads(await tool.handler({"name": "x1", "transport": "http"}))
    assert http_no_url["success"] is False
    # stdio with url
    stdio_with_url = json.loads(
        await tool.handler(
            {"name": "x2", "transport": "stdio", "command_argv": ["a"], "url": "http://x"}
        )
    )
    assert stdio_with_url["success"] is False
    assert await mcp_repo.list_all(conn) == []


async def test_mcp_install_cleans_up_on_crash(conn) -> None:
    """A handshake failure must leave no zombie row behind."""
    manager, connector = _manager(conn)
    connector.raise_on_open["doomed"] = RuntimeError("connection refused")
    tool = _meta_tools(conn, manager=manager)["mcp_install"]

    out = json.loads(
        await tool.handler(
            {"name": "doomed", "transport": "stdio", "command_argv": ["nope"]}
        )
    )
    assert out["success"] is False
    assert "connection refused" in out["error"]
    # Cleanup deleted the row.
    assert await mcp_repo.get_by_name(conn, "doomed") is None


async def test_mcp_install_duplicate_name(conn) -> None:
    manager, connector = _manager(conn)
    connector.configure("dup", ["t"])
    tool = _meta_tools(conn, manager=manager)["mcp_install"]
    first = json.loads(
        await tool.handler({"name": "dup", "transport": "stdio", "command_argv": ["a"]})
    )
    assert first["success"] is True
    second = json.loads(
        await tool.handler({"name": "dup", "transport": "stdio", "command_argv": ["a"]})
    )
    assert second["success"] is False
    assert "already exists" in second["error"]


# --- mcp_restart -----------------------------------------------------------


async def test_mcp_restart_does_not_require_approval(conn) -> None:
    manager, _ = _manager(conn)
    assert _meta_tools(conn, manager=manager)["mcp_restart"].requires_approval is False


async def test_mcp_restart_unknown_name_returns_error(conn) -> None:
    manager, _ = _manager(conn)
    tool = _meta_tools(conn, manager=manager)["mcp_restart"]
    out = json.loads(await tool.handler({"name": "ghost"}))
    assert out["success"] is False
    assert "ghost" in out["error"]


async def test_mcp_restart_relaunches_existing_server(conn) -> None:
    manager, connector = _manager(conn)
    connector.configure("alpha", ["read"])
    created = await mcp_repo.create(
        conn, name="alpha", display_name="Alpha", transport="stdio",
        command_argv=["npx", "alpha"],
    )
    await manager.start_server(created.id)
    tool = _meta_tools(conn, manager=manager)["mcp_restart"]

    out = json.loads(await tool.handler({"name": "alpha"}))
    assert out["success"] is True
    assert out["server"]["status"] == "ready"
    assert out["tools_added"] == ["alpha__read"]
