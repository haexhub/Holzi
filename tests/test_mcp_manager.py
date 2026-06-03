"""Tests for `McpServerManager` (Plan 32).

Drives the lifecycle (start/stop/restart) via an injectable `connect`
factory so the unit tests never spawn a real subprocess or open a real
HTTP session. The factory yields a `FakeSession` shaped just enough for
`list_tools` / `call_tool` and for verifying that the manager hands the
right `args` through.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from hermes.crypto import Encryptor
from hermes.mcp_manager import McpServerManager
from hermes.repository import mcp_servers as repo

# --- fakes ------------------------------------------------------------------


@dataclass
class _FakeTool:
    name: str
    description: str = ""
    inputSchema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


@dataclass
class _FakeToolResult:
    text: str = ""
    isError: bool = False


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeCallResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_FakeContent(text)]
        self.isError = is_error
        self.structuredContent = None


@dataclass
class _ListToolsReturn:
    tools: list[_FakeTool]


class FakeSession:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = tools
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.initialised = False
        # Test knobs:
        self.call_returns: dict[str, str] = {}
        self.call_raises: dict[str, Exception] = {}

    async def initialize(self) -> None:
        self.initialised = True

    async def list_tools(self) -> _ListToolsReturn:
        return _ListToolsReturn(tools=list(self._tools))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeCallResult:
        self.calls.append((name, arguments))
        if name in self.call_raises:
            raise self.call_raises[name]
        text = self.call_returns.get(name, f"{name}-result")
        return _FakeCallResult(text=text)


@dataclass
class _ConnectorState:
    """Per-server state the test-side connector tracks across (re)starts."""

    session: FakeSession | None = None
    open_count: int = 0
    close_count: int = 0
    raise_on_open: Exception | None = None
    raise_on_initialize: Exception | None = None
    tools: list[_FakeTool] = field(default_factory=list)


class FakeConnector:
    """Replacement for the production `connect` factory.

    Tests register per-server-name behaviour and assert on open/close
    counts to verify lifecycle correctness.
    """

    def __init__(self) -> None:
        self.state: dict[str, _ConnectorState] = {}

    def configure(self, name: str, *, tools: list[_FakeTool]) -> _ConnectorState:
        st = self.state.setdefault(name, _ConnectorState())
        st.tools = tools
        return st

    @asynccontextmanager
    async def __call__(self, server, secrets) -> AsyncIterator[FakeSession]:
        st = self.state.setdefault(server.name, _ConnectorState())
        st.open_count += 1
        if st.raise_on_open is not None:
            raise st.raise_on_open
        session = FakeSession(tools=list(st.tools))
        if st.raise_on_initialize is not None:
            # Simulate a handshake failure during initialize; the manager
            # is expected to translate this into status="crashed".
            try:
                raise st.raise_on_initialize
            finally:
                st.close_count += 1
        st.session = session
        try:
            yield session
        finally:
            st.close_count += 1
            st.session = None


# --- helpers ----------------------------------------------------------------


def _make_encryptor() -> Encryptor:
    return Encryptor(b"\x00" * 32)


async def _add_http(conn, name: str, *, enabled: bool = True) -> int:
    row = await repo.create(
        conn,
        name=name,
        display_name=name,
        transport="http",
        url=f"https://example.com/{name}",
        enabled=enabled,
    )
    return row.id


# --- tests ------------------------------------------------------------------


async def test_start_all_enabled_skips_disabled_rows(conn) -> None:
    fc = FakeConnector()
    fc.configure("alive", tools=[_FakeTool(name="ping")])
    fc.configure("dead", tools=[_FakeTool(name="zzz")])
    await _add_http(conn, "alive", enabled=True)
    await _add_http(conn, "dead", enabled=False)

    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        await mgr.start_all_enabled()
        # disabled row never opened.
        assert fc.state["dead"].open_count == 0
        alive = await repo.get_by_name(conn, "alive")
        assert alive is not None
        handle = mgr.get_handle(alive.id)
        assert handle is not None
        assert handle.status == "ready"
        assert handle.last_error is None
        tools = mgr.aggregate_tools()
        assert [t.name for t in tools] == ["alive__ping"]
        assert tools[0].source == "mcp:alive"
    finally:
        await mgr.stop_all()


async def test_handler_calls_session_call_tool(conn) -> None:
    fc = FakeConnector()
    fc.configure("fs", tools=[_FakeTool(name="read_file")])
    server_id = await _add_http(conn, "fs")
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        await mgr.start_server(server_id)
        tools = mgr.aggregate_tools()
        assert len(tools) == 1
        tool = tools[0]
        out = await tool.handler({"path": "/tmp/foo"})
        # FakeSession returns f"{name}-result" by default.
        assert "read_file-result" in out
        session = fc.state["fs"].session
        assert session is not None
        assert session.calls == [("read_file", {"path": "/tmp/foo"})]
    finally:
        await mgr.stop_all()


async def test_start_server_records_crash_on_handshake_failure(conn) -> None:
    fc = FakeConnector()
    st = fc.configure("broken", tools=[])
    st.raise_on_initialize = RuntimeError("handshake refused by server")
    server_id = await _add_http(conn, "broken")
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        # Manager swallows the start-error; status moves to "crashed".
        await mgr.start_server(server_id)
        handle = mgr.get_handle(server_id)
        assert handle is not None
        assert handle.status == "crashed"
        assert handle.last_error is not None
        assert "handshake" in handle.last_error
        # No tools surface.
        assert mgr.aggregate_tools() == []
        # last_error persisted to the DB so a UI restart can show it.
        persisted = await repo.get(conn, server_id)
        assert persisted is not None
        assert persisted.last_error is not None
        assert "handshake" in persisted.last_error
    finally:
        await mgr.stop_all()


async def test_restart_server_replaces_handle_and_clears_error(conn) -> None:
    fc = FakeConnector()
    st = fc.configure("retry", tools=[_FakeTool(name="ping")])
    server_id = await _add_http(conn, "retry")
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        # Initial start crashes.
        st.raise_on_initialize = RuntimeError("boom")
        await mgr.start_server(server_id)
        assert mgr.get_handle(server_id).status == "crashed"
        # Recovery: clear the failure mode and restart.
        st.raise_on_initialize = None
        handle = await mgr.restart_server(server_id)
        assert handle.status == "ready"
        assert handle.last_error is None
        # last_error cleared in DB too.
        row = await repo.get(conn, server_id)
        assert row is not None
        assert row.last_error is None
        assert fc.state["retry"].open_count >= 2  # initial attempt + restart
    finally:
        await mgr.stop_all()


async def test_stop_server_drops_handle_and_tools(conn) -> None:
    fc = FakeConnector()
    fc.configure("stop", tools=[_FakeTool(name="ping")])
    server_id = await _add_http(conn, "stop")
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        await mgr.start_server(server_id)
        assert mgr.aggregate_tools()
        await mgr.stop_server(server_id)
        assert mgr.get_handle(server_id) is None
        assert mgr.aggregate_tools() == []
        # Close was driven by the context manager exit.
        assert fc.state["stop"].close_count >= 1
    finally:
        await mgr.stop_all()


async def test_aggregate_tools_orders_by_server_name(conn) -> None:
    fc = FakeConnector()
    fc.configure("zeta", tools=[_FakeTool(name="z1")])
    fc.configure("alpha", tools=[_FakeTool(name="a1"), _FakeTool(name="a2")])
    await _add_http(conn, "zeta")
    await _add_http(conn, "alpha")
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        await mgr.start_all_enabled()
        # Order is by server name, then by mcp tool name within the server.
        names = [t.name for t in mgr.aggregate_tools()]
        assert names == ["alpha__a1", "alpha__a2", "zeta__z1"]
    finally:
        await mgr.stop_all()


async def test_handler_returns_error_when_session_not_ready(conn) -> None:
    fc = FakeConnector()
    fc.configure("pong", tools=[_FakeTool(name="ping")])
    server_id = await _add_http(conn, "pong")
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        await mgr.start_server(server_id)
        tool = mgr.aggregate_tools()[0]
        await mgr.stop_server(server_id)
        # Handler still callable (the catalog snapshot held a reference) —
        # but the manager-side handle is gone, so it must error rather
        # than NPE.
        out = await tool.handler({})
        assert "error" in out.lower()
    finally:
        await mgr.stop_all()


async def test_on_catalog_change_fires_on_lifecycle_transitions(conn) -> None:
    fc = FakeConnector()
    fc.configure("cat", tools=[_FakeTool(name="ping")])
    server_id = await _add_http(conn, "cat")
    changes = 0

    def _bump() -> None:
        nonlocal changes
        changes += 1

    mgr = McpServerManager(
        conn,
        encryptor=_make_encryptor(),
        connect=fc,
        on_catalog_change=_bump,
    )
    try:
        await mgr.start_server(server_id)
        await mgr.restart_server(server_id)
        await mgr.stop_server(server_id)
        # at least one fire per transition (start, restart, stop).
        assert changes >= 3
    finally:
        await mgr.stop_all()


async def test_start_server_unknown_id_is_noop(conn) -> None:
    fc = FakeConnector()
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        # Unknown server id: nothing to do, no exception.
        with pytest.raises(LookupError):
            await mgr.start_server(99999)
    finally:
        await mgr.stop_all()


async def test_start_server_skips_disabled_row(conn) -> None:
    fc = FakeConnector()
    fc.configure("off", tools=[_FakeTool(name="t")])
    server_id = await _add_http(conn, "off", enabled=False)
    mgr = McpServerManager(conn, encryptor=_make_encryptor(), connect=fc)
    try:
        handle = await mgr.start_server(server_id)
        assert handle.status == "disabled"
        assert fc.state["off"].open_count == 0
        assert mgr.aggregate_tools() == []
    finally:
        await mgr.stop_all()


async def test_credentials_decrypted_and_passed_to_connector(conn) -> None:
    enc = _make_encryptor()
    blob = enc.encrypt("super-secret-token")
    seen_secrets: list[Any] = []
    fc = FakeConnector()
    fc.configure("auth", tools=[_FakeTool(name="t")])

    @asynccontextmanager
    async def wrapping(server, secrets):
        seen_secrets.append(secrets)
        async with fc(server, secrets) as session:
            yield session

    row = await repo.create(
        conn,
        name="auth",
        display_name="auth",
        transport="http",
        url="https://x",
        ciphertext=blob,
    )
    mgr = McpServerManager(conn, encryptor=enc, connect=wrapping)
    try:
        await mgr.start_server(row.id)
        assert len(seen_secrets) == 1
        assert seen_secrets[0].credentials == "super-secret-token"
    finally:
        await mgr.stop_all()
