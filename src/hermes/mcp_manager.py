"""External-MCP-server lifecycle (Plan 32).

Manages a fleet of registered external MCP servers (HTTP StreamableHTTP
or local stdio subprocess). Each enabled server is started during the
lifespan, hand-shaken (`session.initialize()` + `list_tools()`), and its
tools wrapped as `hermes.agent.Tool` instances with
`source="mcp:<server-name>"`. The aggregate list is merged into the
shared `app.state.tool_catalog` by the lifespan.

Lifecycle:
    `start_all_enabled()`     — lifespan hook, idempotent
    `start_server(id)`        — boot one (no-op for disabled rows)
    `stop_server(id)`         — tear one down
    `restart_server(id)`      — stop + start; clears `last_error` on success
    `stop_all()`              — cleanup hook
    `aggregate_tools()`       — all `ready` servers' wrapped tools, flat,
                                ordered by (server_name, tool_name)

No auto-restart and no auto-retry: a crashed handshake leaves the row
with `status="crashed"` and `last_error` set; the user restarts manually
via the UI (consistent with Plan 11b-b sandbox crash semantics).

Single-worker invariant (see `hermes.agent` top-of-file): the in-memory
handle map lives on this manager instance and is read by `app.state`
consumers. There is only ever one process; no locking required.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.agent import Tool
from hermes.crypto import Encryptor
from hermes.logging import logger
from hermes.repository import mcp_servers as repo
from hermes.repository.mcp_servers import McpServerSecrets

McpStatus = Literal["starting", "ready", "crashed", "disabled"]


# Connector contract: factory that, given a row + decrypted secrets, hands
# back an async-context-manager yielding an `mcp.ClientSession`-shaped
# object (must expose `initialize()`, `list_tools()`, `call_tool()`).
ConnectFn = Callable[
    [Any, McpServerSecrets],
    "AbstractAsyncContextManager[Any]",  # noqa: F821 — Any to keep tests stub-friendly
]


@dataclass
class McpServerHandle:
    """Live runtime state for a single registered MCP server.

    The session reference is reassigned by `restart_server`; tool handlers
    look it up by `server_id` rather than capturing a snapshot, so the
    catalog stays correct across restarts.
    """

    id: int
    name: str
    transport: str
    status: McpStatus
    tools: list[Tool] = field(default_factory=list)
    last_error: str | None = None
    last_checked_at: float = 0.0
    # Internals: the per-server lifecycle task and a signal that the
    # context-manager-entered session is ready to call.
    _task: asyncio.Task[None] | None = None
    _stop_event: asyncio.Event | None = None
    _ready_event: asyncio.Event | None = None
    _session: Any | None = None


class McpServerManager:
    """Owns the per-server lifecycle tasks. Single-process / single-user.

    `connect` is the dependency-injection seam for tests: production code
    leaves it at the default and gets stdio/streamable-http; tests pass a
    fake yielding a stub session.
    """

    def __init__(
        self,
        db: AsyncEngine,
        *,
        encryptor: Encryptor,
        connect: ConnectFn | None = None,
        on_catalog_change: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._encryptor = encryptor
        self._connect = connect if connect is not None else _default_connect
        self._on_catalog_change = on_catalog_change
        self._handles: dict[int, McpServerHandle] = {}

    # --- public lifecycle --------------------------------------------------

    async def start_all_enabled(self) -> None:
        rows = await repo.list_enabled(self._db)
        for row in rows:
            try:
                await self.start_server(row.id)
            except Exception as exc:  # noqa: BLE001 — boot continues
                logger.warning(
                    "mcp_server_start_failed",
                    server=row.name,
                    error=str(exc),
                )

    async def stop_all(self) -> None:
        ids = list(self._handles.keys())
        for sid in ids:
            await self.stop_server(sid)

    async def start_server(self, server_id: int) -> McpServerHandle:
        row = await repo.get(self._db, server_id)
        if row is None:
            raise LookupError(f"mcp server {server_id} not found")

        # Already running? No-op return the existing handle.
        existing = self._handles.get(server_id)
        if existing is not None and existing.status in ("starting", "ready"):
            return existing

        if not row.enabled:
            handle = McpServerHandle(
                id=row.id,
                name=row.name,
                transport=row.transport,
                status="disabled",
            )
            self._handles[server_id] = handle
            self._fire_catalog_change()
            return handle

        secrets = await repo.read_secrets(
            self._db, server_id, encryptor=self._encryptor
        )
        if secrets is None:
            raise LookupError(f"mcp server {server_id} secrets not found")

        handle = McpServerHandle(
            id=row.id,
            name=row.name,
            transport=row.transport,
            status="starting",
            _stop_event=asyncio.Event(),
            _ready_event=asyncio.Event(),
        )
        self._handles[server_id] = handle

        handle._task = asyncio.create_task(
            self._run_lifecycle(row, secrets, handle),
            name=f"mcp-server-{row.name}",
        )

        # Wait for the lifecycle task to publish its terminal state for the
        # boot: either "ready" or "crashed". Past that point we hand back
        # the handle and the task keeps the session open until stop.
        await handle._ready_event.wait()
        self._fire_catalog_change()
        return handle

    async def stop_server(self, server_id: int) -> None:
        handle = self._handles.pop(server_id, None)
        if handle is None:
            return
        if handle._stop_event is not None:
            handle._stop_event.set()
        if handle._task is not None:
            with suppress(asyncio.CancelledError):
                await handle._task
        handle.status = "disabled"
        handle._session = None
        self._fire_catalog_change()

    async def restart_server(self, server_id: int) -> McpServerHandle:
        await self.stop_server(server_id)
        return await self.start_server(server_id)

    # --- queries -----------------------------------------------------------

    def get_handle(self, server_id: int) -> McpServerHandle | None:
        return self._handles.get(server_id)

    def list_handles(self) -> list[McpServerHandle]:
        return list(self._handles.values())

    def aggregate_tools(self) -> list[Tool]:
        """Flatten every `ready` server's tool list, ordered by server
        name then mcp tool name. Disabled / crashed servers contribute
        nothing — the catalog stays usable even when one server is dead.
        """
        out: list[Tool] = []
        for h in sorted(self._handles.values(), key=lambda x: x.name):
            if h.status != "ready":
                continue
            out.extend(h.tools)
        return out

    # --- internals ---------------------------------------------------------

    def _fire_catalog_change(self) -> None:
        if self._on_catalog_change is None:
            return
        try:
            self._on_catalog_change()
        except Exception as exc:  # noqa: BLE001 — handler isolation
            logger.warning("mcp_catalog_change_handler_failed", error=str(exc))

    async def _run_lifecycle(
        self,
        row: Any,
        secrets: McpServerSecrets,
        handle: McpServerHandle,
    ) -> None:
        """Open the transport + session, list tools, then wait for stop.

        On any exception during the connect / initialize / list_tools
        phase, the handle moves to `crashed` and `last_error` is set
        (also persisted to the DB). The session stays open for the
        lifetime of `stop_event`.
        """
        try:
            async with self._connect(row, secrets) as session:
                try:
                    await session.initialize()
                    list_result = await session.list_tools()
                except Exception as exc:  # noqa: BLE001 — surface as crash
                    await self._mark_crashed(handle, exc)
                    handle._ready_event.set()
                    return

                tools = _wrap_tools(self, row.name, row.id, list_result.tools)
                handle.tools = tools
                handle.status = "ready"
                handle.last_error = None
                handle._session = session
                await repo.set_last_error(self._db, row.id, None)
                handle._ready_event.set()

                # Keep the context open until stop_server is called.
                await handle._stop_event.wait()
        except Exception as exc:  # noqa: BLE001 — connect itself can fail
            await self._mark_crashed(handle, exc)
            if not handle._ready_event.is_set():
                handle._ready_event.set()

    async def _mark_crashed(
        self, handle: McpServerHandle, exc: BaseException
    ) -> None:
        message = f"{type(exc).__name__}: {exc}"
        handle.status = "crashed"
        handle.tools = []
        handle.last_error = message[:256]
        handle._session = None
        try:
            await repo.set_last_error(self._db, handle.id, message)
        except Exception as inner:  # noqa: BLE001
            logger.warning(
                "mcp_set_last_error_failed", server=handle.name, error=str(inner)
            )
        logger.info("mcp_server_crashed", server=handle.name, error=message)


# ---------------------------------------------------------------------------
# Tool wrapping
# ---------------------------------------------------------------------------


def _wrap_tools(
    manager: McpServerManager,
    server_name: str,
    server_id: int,
    mcp_tools: list[Any],
) -> list[Tool]:
    """Wrap each MCP tool into a hermes `Tool`.

    Names are namespaced as `{server_name}__{tool_name}` to avoid the
    cross-server-collision foot-gun. `source` carries the registered
    server name so the UI can render the right badge.
    """
    wrapped: list[Tool] = []
    for mcp_tool in sorted(mcp_tools, key=lambda t: getattr(t, "name", "")):
        name = f"{server_name}__{mcp_tool.name}"
        description = getattr(mcp_tool, "description", None) or f"MCP tool {name}"
        schema = getattr(mcp_tool, "inputSchema", None) or {"type": "object"}
        handler = _make_handler(manager, server_id, mcp_tool.name)
        wrapped.append(
            Tool(
                name=name,
                description=description,
                parameters_schema=schema,
                handler=handler,
                source=f"mcp:{server_name}",
            )
        )
    return wrapped


def _make_handler(
    manager: McpServerManager,
    server_id: int,
    mcp_tool_name: str,
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    async def handler(args: dict[str, Any]) -> str:
        handle = manager.get_handle(server_id)
        if handle is None or handle.status != "ready" or handle._session is None:
            return f"error: MCP server (id={server_id}) is not ready"
        try:
            result = await handle._session.call_tool(mcp_tool_name, args)
        except Exception as exc:  # noqa: BLE001 — surface to agent
            return f"error: {type(exc).__name__}: {exc}"
        return _extract_text(result)

    handler.__name__ = f"mcp_tool_{mcp_tool_name}"
    return handler


def _extract_text(result: Any) -> str:
    """Best-effort: collect every text-shaped content block, fall back to
    the structured payload, then to the result's repr.
    """
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if parts:
        joined = "\n".join(parts)
        if getattr(result, "isError", False):
            return f"error: {joined}"
        return joined
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        try:
            return json.dumps(structured)
        except (TypeError, ValueError):
            return repr(structured)
    return ""


# ---------------------------------------------------------------------------
# Default connector (real MCP SDK)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _default_connect(
    server: Any, secrets: McpServerSecrets
) -> AsyncIterator[Any]:
    """Production connector. Wraps the MCP SDK clients into a single
    async context that yields a ready `ClientSession`.

    Imports stay local so tests that swap in a fake never pay the SDK
    import cost on the hot path.
    """
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    stack = AsyncExitStack()
    try:
        if server.transport == "http":
            headers: dict[str, str] = {}
            if secrets.credentials:
                headers["Authorization"] = f"Bearer {secrets.credentials}"
            read, write, _close = await stack.enter_async_context(
                streamablehttp_client(server.url, headers=headers or None)
            )
        elif server.transport == "stdio":
            argv = server.command_argv or []
            if not argv:
                raise ValueError("stdio server has empty command_argv")
            params = StdioServerParameters(
                command=argv[0],
                args=list(argv[1:]),
                env=dict(secrets.env) if secrets.env else None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            raise ValueError(f"unknown transport: {server.transport!r}")
        session = await stack.enter_async_context(ClientSession(read, write))
        yield session
    finally:
        await stack.aclose()
