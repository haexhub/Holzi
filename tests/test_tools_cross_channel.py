import json
import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import conversations, messages
from hermes.signal.client import SignalClient
from hermes.tools.cross_channel import build_cross_channel_tools

SELF_NUMBER = "+491701234567"


def _make_signal_client(send_capture: list[dict[str, Any]] | None = None) -> SignalClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/send":
            if send_capture is not None:
                send_capture.append(json.loads(request.content))
            return httpx.Response(201, json={"timestamp": 1700000000000})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake-signal")
    return SignalClient(http, SELF_NUMBER)


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool not found: {name}")


async def test_cross_channel_send_unsupported_channel_returns_error(
    conn: AsyncEngine,
) -> None:
    tools = build_cross_channel_tools(conn, _make_signal_client(), SELF_NUMBER)
    tool = _by_name(tools, "cross_channel_send")
    result = await tool.handler({"channel": "email", "message": "hi"})
    assert "error" in result.lower()


async def test_cross_channel_send_signal_not_configured_returns_error(
    conn: AsyncEngine,
) -> None:
    tools = build_cross_channel_tools(conn, None, None)
    tool = _by_name(tools, "cross_channel_send")
    result = await tool.handler({"channel": "signal", "message": "hi"})
    assert "error" in result.lower()
    assert "signal" in result.lower()


async def test_cross_channel_send_signal_persists_and_sends(
    conn: AsyncEngine,
) -> None:
    sends: list[dict[str, Any]] = []
    tools = build_cross_channel_tools(conn, _make_signal_client(sends), SELF_NUMBER)
    tool = _by_name(tools, "cross_channel_send")

    result = await tool.handler({"channel": "signal", "message": "ping from vscode"})
    data = json.loads(result)
    assert data["sent"] is True

    convos = await conversations.list_by_channel(conn, "signal")
    assert len(convos) == 1
    msgs = await messages.list_by_conversation(conn, convos[0].id)
    assert [(m.role, m.content) for m in msgs] == [("assistant", "ping from vscode")]
    assert sends == [
        {
            "message": "ping from vscode",
            "number": SELF_NUMBER,
            "recipients": [SELF_NUMBER],
        }
    ]


async def test_cross_channel_send_reuses_recent_conversation(
    conn: AsyncEngine,
) -> None:
    one_hour_ago = int(time.time()) - 3600
    existing = await conversations.create(conn, channel="signal", ts=one_hour_ago)

    tools = build_cross_channel_tools(conn, _make_signal_client(), SELF_NUMBER)
    tool = _by_name(tools, "cross_channel_send")
    result = await tool.handler({"channel": "signal", "message": "follow-up"})
    data = json.loads(result)
    assert data["conversation_id"] == existing.id

    convos = await conversations.list_by_channel(conn, "signal")
    assert len(convos) == 1


async def test_cross_channel_send_creates_new_conversation_when_gap_exceeds_6h(
    conn: AsyncEngine,
) -> None:
    seven_hours_ago = int(time.time()) - 7 * 3600
    existing = await conversations.create(conn, channel="signal", ts=seven_hours_ago)

    tools = build_cross_channel_tools(conn, _make_signal_client(), SELF_NUMBER)
    tool = _by_name(tools, "cross_channel_send")
    result = await tool.handler({"channel": "signal", "message": "new thread"})
    data = json.loads(result)
    assert data["conversation_id"] != existing.id

    convos = await conversations.list_by_channel(conn, "signal")
    assert len(convos) == 2


async def test_cross_channel_send_refuses_when_current_channel_matches(
    conn: AsyncEngine,
) -> None:
    """Recursion guard: agent running on channel X cannot write back to X."""
    tools = build_cross_channel_tools(
        conn, _make_signal_client(), SELF_NUMBER, current_channel="signal"
    )
    tool = _by_name(tools, "cross_channel_send")

    result = await tool.handler({"channel": "signal", "message": "would loop"})
    data = json.loads(result)
    assert "error" in data
    assert "signal" in data["error"].lower()

    convos = await conversations.list_by_channel(conn, "signal")
    assert convos == []


async def test_cross_channel_send_allows_different_current_channel(
    conn: AsyncEngine,
) -> None:
    tools = build_cross_channel_tools(
        conn, _make_signal_client(), SELF_NUMBER, current_channel="web"
    )
    tool = _by_name(tools, "cross_channel_send")
    result = await tool.handler({"channel": "signal", "message": "from web"})
    data = json.loads(result)
    assert data.get("sent") is True


async def test_cross_channel_send_does_not_persist_when_signal_send_fails(
    conn: AsyncEngine,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/send":
            return httpx.Response(500, json={"error": "signal-cli down"})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake-signal")
    failing_client = SignalClient(http, SELF_NUMBER)

    tools = build_cross_channel_tools(conn, failing_client, SELF_NUMBER)
    tool = _by_name(tools, "cross_channel_send")

    import httpx as _httpx

    try:
        await tool.handler({"channel": "signal", "message": "won't arrive"})
        raised = False
    except _httpx.HTTPStatusError:
        raised = True
    assert raised, "expected signal_client.send to raise"

    # No conversation should have been touched because the send failed first.
    convos = await conversations.list_by_channel(conn, "signal")
    assert convos == []
