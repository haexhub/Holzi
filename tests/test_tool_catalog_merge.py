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
