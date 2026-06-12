"""Plan 32-A redaction contract for `mcp_install`.

`credentials` + `env` values are secrets and may only travel in plaintext
through the repo/manager write path. This file pins that they are masked
at every other sink:

(a) the `approval_required` SSE event payload (frontend approval card),
(b) the persisted `messages.meta_json` records (assistant tool_calls +
    the tool-role arguments) — agent_runs has no events column, so the
    message rows ARE the persisted tool-call records,
(c) the `meta_tool_invoked` log line (asserted at the logger boundary:
    the handler hands the logger an already-redacted payload, so the
    secret never reaches the Plan-27 redaction processor in the first
    place),

and that the redacted approval event is emitted *before* the tool runs,
so there is no window where a raw payload could leak.
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

from hermes.agent import ApprovalDecision, Tool, _redact_persisted_tool_calls
from hermes.crypto import Encryptor
from hermes.main import app
from hermes.repository import messages
from hermes.tools.meta import build_meta_tools, redact_mcp_install_params

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}

SECRET_CRED = "bearer-xyz"
SECRET_ENV = "ghp_secret"


# --- upstream mock helpers (mirror test_chat_meta_approval.py) --------------


def _assistant_oneshot(content: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
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
        ]
    }


def _to_sse_stream(payload: dict[str, Any]) -> bytes:
    msg = payload["choices"][0]["message"]
    content = msg.get("content")
    tool_calls = msg.get("tool_calls") or []
    out = b""
    if content:
        chunk = {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}
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


def _install_upstream_responses(responses: list[dict[str, Any]]) -> None:
    iter_resp = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = next(iter_resp)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(_to_sse_stream(payload)),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake-proxy"
    )


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


async def _resolve_first_pending_approval(decision: str) -> None:
    for _ in range(500):
        for _, future in list(app.state.approvals.items()):
            if not future.done():
                future.set_result(ApprovalDecision(decision=decision))  # type: ignore[arg-type]
                return
        await asyncio.sleep(0.01)
    raise AssertionError("no approval became pending")


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
        return _ListToolsReturn(tools=[_FakeTool("read_file")])


@asynccontextmanager
async def _fake_connect(server: Any, secrets: Any) -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture
async def client(pg_db):
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


# --- (a) approval event + (b) persisted records + sequence -----------------


async def test_mcp_install_redacts_event_and_persisted_records(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response(
                "mcp_install",
                {
                    "name": "github",
                    "transport": "stdio",
                    "command_argv": ["npx", "server-github"],
                    "env": {"GITHUB_TOKEN": SECRET_ENV},
                    "credentials": SECRET_CRED,
                },
            ),
            _assistant_oneshot("Installed."),
        ]
    )
    resolver = asyncio.create_task(_resolve_first_pending_approval("allow_once"))
    body = b""
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "install github mcp"}
    ) as response:
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    events = _parse_sse(body)
    names = [n for n, _ in events]

    # (a) approval card payload is redacted but keeps the env KEY visible.
    appr = next(d for n, d in events if n == "approval_required")
    appr_blob = json.dumps(appr)
    assert SECRET_CRED not in appr_blob
    assert SECRET_ENV not in appr_blob
    assert appr["arguments"]["credentials"] == f"[redacted, {len(SECRET_CRED)} chars]"
    assert appr["arguments"]["env"] == {"GITHUB_TOKEN": "[redacted]"}

    # Sequence: the (redacted) approval event precedes the tool execution —
    # no window where a raw payload could have been emitted first.
    assert names.index("approval_required") < names.index("tool_result")
    # The whole stream never carries a raw secret.
    assert SECRET_CRED not in body.decode()
    assert SECRET_ENV not in body.decode()

    # (b) persisted message rows (assistant tool_calls + tool args) redacted.
    session_evt = next(d for n, d in events if n == "session")
    conv_id = session_evt["conversation_id"]
    rows = await messages.list_by_conversation(app.state.db, conv_id, user_id=1)
    meta_blob = "\n".join(r.meta_json for r in rows if r.meta_json)
    assert SECRET_CRED not in meta_blob
    assert SECRET_ENV not in meta_blob

    # And concretely: both record shapes carry the masked values.
    tool_row = next(
        r for r in rows if r.meta_json and json.loads(r.meta_json).get("name") == "mcp_install"
    )
    tool_args = json.loads(tool_row.meta_json)["arguments"]
    assert tool_args["credentials"].startswith("[redacted")
    assert tool_args["env"] == {"GITHUB_TOKEN": "[redacted]"}

    assistant_row = next(
        r for r in rows if r.meta_json and json.loads(r.meta_json).get("tool_calls")
    )
    persisted_call = json.loads(assistant_row.meta_json)["tool_calls"][0]
    persisted_args = json.loads(persisted_call["function"]["arguments"])
    assert persisted_args["credentials"].startswith("[redacted")
    assert persisted_args["env"] == {"GITHUB_TOKEN": "[redacted]"}


# --- (c) log line is handed an already-redacted payload --------------------


async def test_mcp_install_log_line_is_redacted(conn, monkeypatch) -> None:
    """The handler hands the logger redacted params, so the secret never
    reaches the structlog pipe. We spy at the logger boundary — stronger
    and more deterministic than scraping rendered output, and the Plan-27
    redaction processor is defense-in-depth tested separately."""
    captured: list[tuple[str, dict[str, Any]]] = []

    class _SpyLogger:
        def info(self, event: str, **kw: Any) -> None:
            captured.append((event, kw))

        def warning(self, *a: Any, **k: Any) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr("hermes.tools.meta.logger", _SpyLogger())

    tools = {
        t.name: t
        for t in build_meta_tools(
            db=conn,
            mcp_manager=None,  # log fires before the manager is touched
            encryptor=Encryptor(b"\x00" * 32),
            tool_catalog_provider=list,
        )
    }
    await tools["mcp_install"].handler(
        {
            "name": "github",
            "transport": "stdio",
            "command_argv": ["npx", "server-github"],
            "env": {"GITHUB_TOKEN": SECRET_ENV},
            "credentials": SECRET_CRED,
        }
    )

    invoked = next(kw for event, kw in captured if event == "meta_tool_invoked")
    blob = json.dumps(invoked)
    assert SECRET_CRED not in blob
    assert SECRET_ENV not in blob
    assert invoked["params"]["credentials"] == f"[redacted, {len(SECRET_CRED)} chars]"
    assert invoked["params"]["env"] == {"GITHUB_TOKEN": "[redacted]"}
    # The env KEY stays visible for transparency.
    assert "GITHUB_TOKEN" in blob


# --- _redact_persisted_tool_calls: never persist raw args (any shape) ------


async def _noop_handler(args: dict[str, Any]) -> str:  # pragma: no cover
    return "ok"


def _install_tool() -> Tool:
    return Tool(
        name="mcp_install",
        description="d",
        parameters_schema={},
        handler=_noop_handler,
        requires_approval=True,
        redact_arguments=redact_mcp_install_params,
    )


def test_persisted_tool_calls_redact_dict_shaped_arguments() -> None:
    """Some non-streaming providers hand `function.arguments` as a dict, not a
    JSON string — it must still be redacted before persistence."""
    call = {
        "id": "c1",
        "type": "function",
        "function": {
            "name": "mcp_install",
            "arguments": {
                "name": "github",
                "transport": "http",
                "url": "https://x",
                "credentials": SECRET_CRED,
                "env": {"GITHUB_TOKEN": SECRET_ENV},
            },
        },
    }
    out = _redact_persisted_tool_calls([call], {"mcp_install": _install_tool()})
    blob = json.dumps(out)
    assert SECRET_CRED not in blob
    assert SECRET_ENV not in blob
    args = json.loads(out[0]["function"]["arguments"])
    assert args["credentials"].startswith("[redacted")
    assert args["env"] == {"GITHUB_TOKEN": "[redacted]"}


def test_persisted_tool_calls_placeholder_for_unparseable_secret_args() -> None:
    """A malformed args string for a secret-bearing tool is replaced with a
    placeholder — never persisted raw (it could still embed the secret)."""
    call = {
        "id": "c2",
        "type": "function",
        "function": {
            "name": "mcp_install",
            "arguments": '{"credentials": "' + SECRET_CRED + '"',  # truncated JSON
        },
    }
    out = _redact_persisted_tool_calls([call], {"mcp_install": _install_tool()})
    assert SECRET_CRED not in json.dumps(out)
    assert json.loads(out[0]["function"]["arguments"]) == {"_redacted": True}


def test_persisted_tool_calls_pass_through_non_redacted_tools() -> None:
    tool = Tool(
        name="save_note", description="d", parameters_schema={}, handler=_noop_handler
    )
    call = {"id": "c", "function": {"name": "save_note", "arguments": '{"k": "v"}'}}
    out = _redact_persisted_tool_calls([call], {"save_note": tool})
    assert out[0] is call  # untouched passthrough
