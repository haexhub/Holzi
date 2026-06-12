import asyncio
import json
from typing import Any

import httpx

from hermes.agent import ApprovalDecision
from hermes.main import app
from tests._chat_sse import (
    assistant_oneshot as _assistant_oneshot,
)
from tests._chat_sse import (
    install_upstream_responses as _install_upstream_responses,
)
from tests._chat_sse import (
    parse_sse as _parse_sse,
)
from tests._chat_sse import (
    parse_sse_envelopes as _parse_sse_envelopes,
)

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _tool_call_first_response(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "x",
        "model": "claude-opus-4-7",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_evt",
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


# ---------------------------------------------------------------------------
# Event envelope + tool call/result events (Plan 08)
# ---------------------------------------------------------------------------


async def test_api_chat_wraps_every_event_in_versioned_envelope(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("hi there")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    envelopes = _parse_sse_envelopes(body)
    assert [name for name, _ in envelopes] == ["session", "run", "text", "done"]
    for sse_event_line, env in envelopes:
        # SSE event line mirrors the envelope's `event` field.
        assert env["event"] == sse_event_line
        assert env["version"] == 1
        assert "data" in env


async def test_api_chat_emits_tool_call_and_result_events(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response("save_note", {"key": "k1", "content": "hello"}),
            _assistant_oneshot("saved it"),
        ]
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "save a note"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert names == ["session", "run", "tool_call", "tool_result", "text", "done"]

    tool_call = next(d for n, d in events if n == "tool_call")
    assert tool_call["call_id"] == "call_evt"
    assert tool_call["name"] == "save_note"
    assert tool_call["arguments"] == {"key": "k1", "content": "hello"}
    assert tool_call["status"] == "running"

    tool_result = next(d for n, d in events if n == "tool_result")
    assert tool_result["call_id"] == "call_evt"
    assert tool_result["status"] == "success"
    assert tool_result["result"]


async def test_conversation_detail_exposes_tool_call_metadata(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses(
        [
            _tool_call_first_response("save_note", {"key": "k2", "content": "x"}),
            _assistant_oneshot("done"),
        ]
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "go"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    conv_id = _parse_sse(body)[0][1]["conversation_id"]

    detail = await client.get(f"/api/conversations/{conv_id}", headers=AUTH)
    assert detail.status_code == 200
    msgs = detail.json()["messages"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    tc = tool_msgs[0]["tool_call"]
    assert tc["call_id"] == "call_evt"
    assert tc["name"] == "save_note"
    assert tc["arguments"] == {"key": "k2", "content": "x"}
    assert tc["status"] == "success"
    assert tc["result"]
    assert tc["error"] is None
    # Non-tool messages carry a null tool_call.
    assert all(m.get("tool_call") is None for m in msgs if m["role"] != "tool")


# ---------------------------------------------------------------------------
# Reasoning (Plan 10)
# ---------------------------------------------------------------------------


def _install_reasoning_upstream() -> None:
    """Install an upstream that streams two reasoning deltas then the answer."""

    def _delta(delta: dict[str, Any]) -> bytes:
        chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        return f"data: {json.dumps(chunk)}\n\n".encode()

    body = (
        _delta({"reasoning_content": "Let me "})
        + _delta({"reasoning_content": "think."})
        + _delta({"content": "42"})
        + b"data: [DONE]\n\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )


async def test_api_chat_emits_reasoning_events_and_persists(
    client: httpx.AsyncClient,
) -> None:
    _install_reasoning_upstream()

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "what is the answer"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    names = [name for name, _ in events]
    # Reasoning events stream before the text answer; chat still ends on done.
    assert names == ["session", "run", "reasoning", "reasoning", "text", "done"]
    reasoning = [d["content"] for n, d in events if n == "reasoning"]
    assert reasoning == ["Let me ", "think."]
    assert next(d for n, d in events if n == "text")["content"] == "42"

    # Reload reconstructs the reasoning from meta_json so the card re-renders.
    conv_id = events[0][1]["conversation_id"]
    detail = await client.get(f"/api/conversations/{conv_id}", headers=AUTH)
    assert detail.status_code == 200
    msgs = detail.json()["messages"]
    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert assistant["reasoning"] == "Let me think."
    # A plain user turn carries no reasoning.
    user = next(m for m in msgs if m["role"] == "user")
    assert user["reasoning"] is None


# ---------------------------------------------------------------------------
# Approvals (Plan 09)
# ---------------------------------------------------------------------------


async def _resolve_first_pending_approval(decision: str) -> str:
    """Wait for an approval future to appear on app.state.approvals and
    resolve it directly.

    httpx's ASGITransport buffers the whole SSE body before returning, so a
    real mid-stream POST /api/approvals isn't testable (same limitation the
    cancel-flow test documents). Resolving the future from a concurrent task
    exercises the emit→pause→resume path end-to-end; the endpoint itself is
    covered by the dedicated unit tests below.
    """
    for _ in range(500):
        for approval_id, future in list(app.state.approvals.items()):
            if not future.done():
                future.set_result(ApprovalDecision(decision=decision))  # type: ignore[arg-type]
                return approval_id
        await asyncio.sleep(0.01)
    raise AssertionError("no approval became pending")


async def test_api_chat_emits_approval_required_and_resumes_on_allow(
    client: httpx.AsyncClient,
) -> None:
    """A risky tool emits `approval_required`; on allow the agent runs it and
    the turn finishes with the normal tool_call/tool_result/text/done tail."""
    _install_upstream_responses(
        [
            _tool_call_first_response(
                "mcp_install",
                {
                    "name": "test-mcp",
                    "display_name": "Test",
                    "transport": "http",
                    "url": "http://127.0.0.1:1/mcp",
                },
            ),
            _assistant_oneshot("done"),
        ]
    )

    resolver = asyncio.create_task(_resolve_first_pending_approval("allow_once"))
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "ping me"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert "approval_required" in names
    # Approval precedes the tool call it gates.
    assert names.index("approval_required") < names.index("tool_call")
    assert names[-1] == "done"

    approval = next(d for n, d in events if n == "approval_required")
    assert approval["call_id"] == "call_evt"
    assert approval["name"] == "mcp_install"
    assert approval["arguments"] == {
        "name": "test-mcp",
        "display_name": "Test",
        "transport": "http",
        "url": "http://127.0.0.1:1/mcp",
    }
    assert approval["approval_id"]
    assert approval["reason"]

    # Registry drained once the decision landed.
    assert approval["approval_id"] not in app.state.approvals


async def test_api_chat_deny_skips_tool_and_feeds_denied_result(
    client: httpx.AsyncClient,
) -> None:
    """On deny the gated tool never runs; the LLM's next round sees a denied
    tool result and no tool_call event is emitted for it."""
    seen = _install_upstream_responses(
        [
            _tool_call_first_response(
                "mcp_install",
                {
                    "name": "test-mcp",
                    "display_name": "Test",
                    "transport": "http",
                    "url": "http://127.0.0.1:1/mcp",
                },
            ),
            _assistant_oneshot("ok, won't send"),
        ]
    )

    resolver = asyncio.create_task(_resolve_first_pending_approval("deny"))
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "ping me"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert "approval_required" in names
    # Denied: nothing executed, so no tool_call event was streamed.
    assert "tool_call" not in names

    # The second upstream round received a denied tool result.
    second_req = seen[1]
    tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "deni" in tool_msgs[0]["content"].lower()


async def test_approval_endpoint_resolves_future_and_returns_204(
    client: httpx.AsyncClient,
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    app.state.approvals["unit-approval"] = future
    try:
        resp = await client.post(
            "/api/approvals/unit-approval",
            headers=AUTH,
            json={"decision": "allow_once"},
        )
        assert resp.status_code == 204
        assert future.done()
        assert future.result().decision == "allow_once"
    finally:
        app.state.approvals.pop("unit-approval", None)


async def test_approval_endpoint_unknown_id_returns_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/approvals/nope", headers=AUTH, json={"decision": "allow_once"}
    )
    assert resp.status_code == 404


async def test_approval_endpoint_already_resolved_returns_409(
    client: httpx.AsyncClient,
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    future.set_result(ApprovalDecision(decision="allow_once"))
    app.state.approvals["done-approval"] = future
    try:
        resp = await client.post(
            "/api/approvals/done-approval",
            headers=AUTH,
            json={"decision": "deny"},
        )
        assert resp.status_code == 409
    finally:
        app.state.approvals.pop("done-approval", None)


async def test_approval_endpoint_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/approvals/anything", json={"decision": "allow_once"}
    )
    assert resp.status_code == 401


async def test_approval_endpoint_rejects_invalid_decision(
    client: httpx.AsyncClient,
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    app.state.approvals["bad-approval"] = future
    try:
        resp = await client.post(
            "/api/approvals/bad-approval",
            headers=AUTH,
            json={"decision": "maybe"},
        )
        assert resp.status_code == 422
        assert not future.done()
    finally:
        app.state.approvals.pop("bad-approval", None)
