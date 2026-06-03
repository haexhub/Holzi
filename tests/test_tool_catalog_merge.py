"""Tests for `build_tool_catalog` MCP merge (Plan 32).

Without a manager, the catalog is built-in only; every built-in carries
`source="builtin"` from the `Tool` default. With a manager, its
`aggregate_tools()` output appends to the built-in list and keeps its
own `source="mcp:<server-name>"` markers.
"""
from __future__ import annotations

from collections.abc import Awaitable

import pytest

from hermes.agent import Tool
from hermes.tool_catalog import build_tool_catalog


class _FakeMcpManager:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = tools

    def aggregate_tools(self) -> list[Tool]:
        return list(self._tools)


def _identity_handler(args: dict) -> Awaitable[str]:
    async def inner() -> str:
        return "ok"

    return inner()


@pytest.mark.asyncio
async def test_builtin_only_marks_all_source_builtin(conn) -> None:
    catalog = build_tool_catalog(
        db=conn,
        signal_client=None,
        signal_self_number=None,
        external_http=None,
        brave_api_key=None,
        mcp_manager=None,
    )
    assert catalog
    assert all(t.source == "builtin" for t in catalog)


@pytest.mark.asyncio
async def test_mcp_tools_appended_after_builtin(conn) -> None:
    remote = Tool(
        name="filesystem__read_file",
        description="MCP tool",
        parameters_schema={"type": "object"},
        handler=_identity_handler,
        source="mcp:filesystem",
    )
    catalog = build_tool_catalog(
        db=conn,
        signal_client=None,
        signal_self_number=None,
        external_http=None,
        brave_api_key=None,
        mcp_manager=_FakeMcpManager([remote]),
    )
    builtin_count = sum(1 for t in catalog if t.source == "builtin")
    assert builtin_count > 0
    assert catalog[-1].name == "filesystem__read_file"
    assert catalog[-1].source == "mcp:filesystem"


@pytest.mark.asyncio
async def test_meta_tools_present_and_builtin(conn) -> None:
    """Plan 32-A: the four meta-tools join the built-ins with source=builtin
    and the expected approval gating."""
    catalog = build_tool_catalog(
        db=conn,
        signal_client=None,
        signal_self_number=None,
        external_http=None,
        brave_api_key=None,
        mcp_manager=None,
    )
    by_name = {t.name: t for t in catalog}
    for name in ("list_tools", "mcp_status", "mcp_install", "mcp_restart"):
        assert name in by_name, f"missing meta-tool {name}"
        assert by_name[name].source == "builtin"
    # mcp_install is the only approval-gated meta-tool.
    assert by_name["mcp_install"].requires_approval is True
    assert by_name["mcp_restart"].requires_approval is False
    assert by_name["list_tools"].requires_approval is False
    assert by_name["mcp_status"].requires_approval is False


@pytest.mark.asyncio
async def test_list_tools_reflects_provider(conn) -> None:
    """`list_tools` reads the catalog at call time via the provider, so a
    server installed after boot shows up (no stale closure)."""
    import json

    captured: dict[str, list[Tool]] = {"catalog": []}
    catalog = build_tool_catalog(
        db=conn,
        signal_client=None,
        signal_self_number=None,
        external_http=None,
        brave_api_key=None,
        mcp_manager=None,
        tool_catalog_provider=lambda: captured["catalog"],
    )
    list_tools = next(t for t in catalog if t.name == "list_tools")
    assert json.loads(await list_tools.handler({}))["tools"] == []
    captured["catalog"] = [
        Tool(
            name="filesystem__read_file",
            description="d",
            parameters_schema={},
            handler=_identity_handler,
            source="mcp:filesystem",
        )
    ]
    out = json.loads(await list_tools.handler({}))
    assert [t["name"] for t in out["tools"]] == ["filesystem__read_file"]


@pytest.mark.asyncio
async def test_no_manager_is_equivalent_to_empty_manager(conn) -> None:
    a = build_tool_catalog(
        db=conn,
        signal_client=None,
        signal_self_number=None,
        external_http=None,
        brave_api_key=None,
        mcp_manager=None,
    )
    b = build_tool_catalog(
        db=conn,
        signal_client=None,
        signal_self_number=None,
        external_http=None,
        brave_api_key=None,
        mcp_manager=_FakeMcpManager([]),
    )
    assert [t.name for t in a] == [t.name for t in b]
