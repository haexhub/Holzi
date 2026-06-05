from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.tools.external import build_external_tools
from hermes.tools.memory import build_memory_tools
from hermes.tools.meta import build_meta_tools
from hermes.tools.productivity import build_productivity_tools
from hermes.tools.skills import build_skill_tools
from hermes.tools.user_guide import build_user_guide_tools

if TYPE_CHECKING:  # pragma: no cover
    from hermes.crypto import Encryptor
    from hermes.mcp_manager import McpServerManager


def build_tool_catalog(
    *,
    db: AsyncEngine,
    external_http: httpx.AsyncClient | None,
    brave_api_key: str | None,
    mcp_manager: "McpServerManager | None" = None,
    encryptor: "Encryptor | None" = None,
    tool_catalog_provider: Callable[[], list[Tool]] | None = None,
) -> list[Tool]:
    """Assemble the full Hermes tool catalog.

    `mcp_manager` is the Plan-32 external-MCP-server lifecycle manager.
    Its `aggregate_tools()` output is appended after the built-ins; tools
    keep their `source="mcp:<server-name>"` markers. None disables the
    merge (used by tests and by the catalog snapshot built before the
    manager itself is constructed during lifespan).

    Plan 32-A meta-tools (`list_tools`, `mcp_status`, `mcp_install`,
    `mcp_restart`) join the built-ins. `tool_catalog_provider` lets
    `list_tools` reflect the *current* `app.state.tool_catalog` at call
    time (so a freshly-installed server shows up) instead of closing over
    a stale list; `encryptor` lets `mcp_install` persist credentials.
    """
    # Default to an empty-catalog provider: callers that don't wire one (a few
    # tests) just get a `list_tools` that reports nothing, which is harmless.
    provider = tool_catalog_provider if tool_catalog_provider is not None else (lambda: [])
    builtin: list[Tool] = (
        build_memory_tools(db)
        + build_productivity_tools(db)
        + build_external_tools(external_http, brave_api_key)
        + build_user_guide_tools()
        + build_meta_tools(
            db=db,
            mcp_manager=mcp_manager,
            encryptor=encryptor,
            tool_catalog_provider=provider,
        )
        + build_skill_tools(db)
    )
    # `Tool.source` defaults to "builtin" on the dataclass — every builder
    # leaves it at the default, so the merge needs no explicit annotation
    # pass here. If a future builder ever sets a different source on its
    # tools, this comment is the place to revisit.
    if mcp_manager is None:
        return builtin
    return builtin + mcp_manager.aggregate_tools()
