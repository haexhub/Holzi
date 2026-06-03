"""Plan 32-A integration: agent-driven `mcp_install` through /api/chat.

Drives the conversational self-provisioning loop end to end: the model
emits an `mcp_install` tool call → the approval gate surfaces an
`approval_required` event → the user approves → the MCP server starts via
the injected fake connector → its tools land in `app.state.tool_catalog`
(via the manager's `on_catalog_change` hook) and would be visible to
`list_tools`. Also covers the deny path (no DB write) and that the
read-only meta-tools run without an approval card.

Mirrors the SSE test harness in test_approvals.py / test_api_chat.py
(helpers duplicated deliberately to keep the file self-contained).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.agent import ApprovalDecision
from hermes.main import app
from hermes.repository import mcp_servers as mcp_repo

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


# --- upstream mock helpers (mirror test_approvals.py) ----------------------


def _assistant_oneshot(content: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _tool_call_first_response(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_meta",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def _to_sse_stream(payload: dict[str, Any]) -> bytes:
    msg = payload["choices"][0]["message"]
    content = msg.get("content")
    tool_calls = msg.get("tool_calls") or []
    out = b""
    if content:
        chunk = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": None}
            ]
        }
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    if tool_calls:
        delta_tcs = [
            {
                "index": i,
                "id": tc["id"],
                "type": tc.get("type", "function"),
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                },
            }
            for i, tc in enumerate(tool_calls)
        ]
        chunk = {
            "choices": [
                {"index": 0, "delta": {"tool_calls": delta_tcs}, "finish_reason": None}
            ]
        }
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    out += b"data: [DONE]\n\n"
    return out


def _install_upstream_responses(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    iter_resp = iter(responses)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        try:
            payload = next(iter_resp)
        except StopIteration as exc:
            raise AssertionError("upstream called more times than expected") from exc
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(_to_sse_stream(payload)),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake-proxy"
    )
    return seen


def _parse_sse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split(b"\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split(b"\n"):
            line = line.strip()
            if line.startswith(b"event: "):
                event = line[len(b"event: ") :].decode()
            elif line.startswith(b"data: "):
                data_lines.append(line[len(b"data: ") :].decode())
        if event:
            data = json.loads("\n".join(data_lines)) if data_lines else {}
            events.append((event, data.get("data", {})))
    return events


async def _resolve_first_pending_approval(
    decision: str, reason: str | None = None
) -> str:
    for _ in range(500):
        for approval_id, future in list(app.state.approvals.items()):
            if not future.done():
                future.set_result(
                    ApprovalDecision(decision=decision, reason=reason)  # type: ignore[arg-type]
                )
                return approval_id
        await asyncio.sleep(0.01)
    raise AssertionError("no approval became pending")


# --- fake MCP connector (yields a fixed tool pair for any server) ----------


@dataclass
class _FakeTool:
    name: str
    description: str = "fake mcp tool"
    inputSchema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


@dataclass
class _ListToolsReturn:
    tools: list[_FakeTool]


class _FakeSession:
    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _ListToolsReturn:
        return _ListToolsReturn(tools=[_FakeTool("read_file"), _FakeTool("write_file")])


@asynccontextmanager
async def _fake_connect(server: Any, secrets: Any) -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c,
    ):
        manager = app.state.mcp_servers_manager
        original = manager._connect
        manager._connect = _fake_connect
        try:
            yield c
        finally:
            manager._connect = original


async def _drain(client: httpx.AsyncClient, message: str) -> bytes:
    body = b""
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": message}
    ) as response:
        assert response.status_code == 200
        async for chunk in response.aiter_bytes():
            body += chunk
    return body


# --- tests -----------------------------------------------------------------


async def test_mcp_install_approve_starts_server_and_updates_catalog(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response(
                "mcp_install",
                {
                    "name": "filesystem",
                    "transport": "stdio",
                    "command_argv": ["npx", "server-filesystem", "/tmp"],
                },
            ),
            _assistant_oneshot("Installed the filesystem server (2 tools)."),
        ]
    )
    resolver = asyncio.create_task(_resolve_first_pending_approval("allow_once"))
    body = await _drain(client, "install the filesystem mcp for /tmp")
    await resolver

    events = _parse_sse(body)
    names = [n for n, _ in events]
    assert "approval_required" in names
    appr = next(d for n, d in events if n == "approval_required")
    assert appr["name"] == "mcp_install"
    assert appr["arguments"]["transport"] == "stdio"
    # tool ran and reported success
    result = next(d for n, d in events if n == "tool_result")
    assert result["status"] == "success"
    assert json.loads(result["result"])["success"] is True

    # server is registered + ready
    row = await mcp_repo.get_by_name(app.state.db, "filesystem")
    assert row is not None
    handle = app.state.mcp_servers_manager.get_handle(row.id)
    assert handle is not None and handle.status == "ready"

    # on_catalog_change refreshed app.state.tool_catalog (what list_tools reads)
    catalog_names = [t.name for t in app.state.tool_catalog]
    assert "filesystem__read_file" in catalog_names
    assert "filesystem__write_file" in catalog_names


async def test_mcp_install_deny_writes_no_server(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response(
                "mcp_install",
                {
                    "name": "filesystem",
                    "transport": "stdio",
                    "command_argv": ["npx", "server-filesystem", "/tmp"],
                },
            ),
            _assistant_oneshot("Understood — not installing it."),
        ]
    )
    resolver = asyncio.create_task(
        _resolve_first_pending_approval("deny", reason="not right now")
    )
    body = await _drain(client, "install filesystem")
    await resolver

    names = [n for n, _ in _parse_sse(body)]
    assert "approval_required" in names
    # Denied → no row created, manager has no handle.
    assert await mcp_repo.get_by_name(app.state.db, "filesystem") is None
    assert app.state.mcp_servers_manager.list_handles() == []


async def test_list_tools_runs_without_approval(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response("list_tools", {}),
            _assistant_oneshot("I have several built-in tools."),
        ]
    )
    body = await _drain(client, "what can you do?")

    events = _parse_sse(body)
    names = [n for n, _ in events]
    assert "approval_required" not in names
    assert "tool_call" in names
    result = next(d for n, d in events if n == "tool_result")
    payload = json.loads(result["result"])
    tool_names = {t["name"] for t in payload["tools"]}
    # The meta-tools list themselves (transparency, no recursion filter).
    assert {"list_tools", "mcp_status", "mcp_install", "mcp_restart"} <= tool_names
