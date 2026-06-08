"""Tests for the /ws/agent WebSocket endpoint (Plan 41)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


# ---------------------------------------------------------------------------
# Upstream mock helpers (streaming, as ws_agent always passes on_chunk)
# ---------------------------------------------------------------------------


def _stream_handler(deltas: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        events: list[bytes] = []
        for d in deltas:
            chunk = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}],
            }
            events.append(f"data: {json.dumps(chunk)}\n\n".encode())
        events.append(b"data: [DONE]\n\n")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(events)),
        )

    return handler


def _stream_tool_call_handler(
    tool_name: str, tool_id: str, args: dict, final_text: str = "Done"
):
    """Upstream mock: first call returns a streaming tool_call, second returns text."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))

        if not has_tool_result:
            tc_chunk = json.dumps({
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(args),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            })
            events = [f"data: {tc_chunk}\n\n".encode(), b"data: [DONE]\n\n"]
        else:
            text_chunk = json.dumps({
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "choices": [{
                    "index": 0,
                    "delta": {"content": final_text},
                    "finish_reason": "stop",
                }],
            })
            events = [f"data: {text_chunk}\n\n".encode(), b"data: [DONE]\n\n"]

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"".join(events)),
        )

    return handler


def _install_upstream(handler):
    transport = httpx.MockTransport(handler)
    app.state.upstream = httpx.AsyncClient(
        transport=transport, base_url="http://fake-proxy"
    )


def _drain_until_done(ws) -> list[dict]:
    """Receive messages until stream_done; return all received messages."""
    msgs: list[dict] = []
    while True:
        msg = ws.receive_json()
        msgs.append(msg)
        if msg["type"] == "stream_done":
            break
    return msgs


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


def test_ws_agent_rejects_missing_token():
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/ws/agent") as ws,
    ):
        ws.receive_json()
    assert exc_info.value.code == 4001


def test_ws_agent_rejects_invalid_token():
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer wrong"}
        ) as ws,
    ):
        ws.receive_json()
    assert exc_info.value.code == 4001


def test_ws_agent_accepts_valid_bearer_header():
    with TestClient(app) as client:
        _install_upstream(_stream_handler(["hello"]))
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({"type": "start_session", "model": "test-model", "tools": []})
            ws.send_json({"type": "message", "content": "hi"})
            _drain_until_done(ws)


def test_ws_agent_accepts_token_query_param():
    with TestClient(app) as client:
        _install_upstream(_stream_handler(["hello"]))
        with client.websocket_connect(f"/ws/agent?token={VALID_TOKEN}") as ws:
            ws.send_json({"type": "start_session", "model": "test-model", "tools": []})
            ws.send_json({"type": "message", "content": "hi"})
            _drain_until_done(ws)


# ---------------------------------------------------------------------------
# Basic message flow
# ---------------------------------------------------------------------------


def test_ws_agent_message_returns_stream_chunks_then_done():
    with TestClient(app) as client:
        _install_upstream(_stream_handler(["Hello", " world"]))
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({"type": "start_session", "model": "test-model", "tools": []})
            ws.send_json({"type": "message", "content": "say hello"})
            msgs = _drain_until_done(ws)

    types = [m["type"] for m in msgs]
    assert "stream_chunk" in types
    assert types[-1] == "stream_done"
    deltas = [m["delta"] for m in msgs if m["type"] == "stream_chunk"]
    assert "".join(deltas) == "Hello world"


def test_ws_agent_start_session_creates_vscode_conversation():
    """Verifies a conversation with channel=vscode is queryable after the turn."""
    with TestClient(app) as client:
        _install_upstream(_stream_handler(["ok"]))
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({"type": "start_session", "model": "test-model", "tools": []})
            ws.send_json({"type": "message", "content": "hello"})
            _drain_until_done(ws)

        # The REST API should list the conversation under channel=vscode
        resp = client.get("/api/conversations?channel=vscode&limit=5", headers=AUTH)
        assert resp.status_code == 200
        convs = resp.json()  # plain list
        assert len(convs) == 1
        assert convs[0]["channel"] == "vscode"


# ---------------------------------------------------------------------------
# Tool call round-trip
# ---------------------------------------------------------------------------


def test_ws_agent_tool_call_round_trip():
    """Agent sends tool_call; client returns tool_result; agent continues."""
    with TestClient(app) as client:
        _install_upstream(
            _stream_tool_call_handler("read_file", "call-1", {"path": "x.py"}, "Done")
        )
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({
                "type": "start_session",
                "model": "test-model",
                "tools": ["read_file"],
            })
            ws.send_json({"type": "message", "content": "read x.py"})

            # Expect tool_call
            msg = ws.receive_json()
            assert msg["type"] == "tool_call"
            assert msg["name"] == "read_file"
            assert msg["params"] == {"path": "x.py"}
            call_id = msg["id"]

            # Return tool result
            ws.send_json({
                "type": "tool_result",
                "id": call_id,
                "result": "def hello(): pass",
            })

            # Agent finishes
            _drain_until_done(ws)


def test_ws_agent_user_denied_allows_graceful_response():
    """error: user_denied is fed to the agent; it should respond, not crash."""
    with TestClient(app) as client:
        _install_upstream(
            _stream_tool_call_handler(
                "read_file", "call-2", {"path": "secret.py"}, "I understand"
            )
        )
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({
                "type": "start_session",
                "model": "test-model",
                "tools": ["read_file"],
            })
            ws.send_json({"type": "message", "content": "read secret.py"})

            msg = ws.receive_json()
            assert msg["type"] == "tool_call"
            call_id = msg["id"]

            # Deny
            ws.send_json({"type": "tool_result", "id": call_id, "error": "user_denied"})

            # No crash; stream_done eventually arrives
            _drain_until_done(ws)


# ---------------------------------------------------------------------------
# Plan mode
# ---------------------------------------------------------------------------


def test_ws_agent_plan_mode_blocks_write_tools():
    """In plan mode, write_file is not forwarded as tool_call to the client."""
    with TestClient(app) as client:
        _install_upstream(
            _stream_tool_call_handler(
                "write_file", "call-3", {"path": "a.py", "content": "x"}, "I would write it"
            )
        )
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({
                "type": "start_session",
                "model": "test-model",
                "permission_mode": "plan",
                "tools": ["read_file", "write_file"],
            })
            ws.send_json({"type": "message", "content": "write something"})

            # Server must NOT send a tool_call; agent gets the description string
            # and the LLM produces the final text → stream_done arrives without
            # the client ever having to handle a tool_call.
            msgs = _drain_until_done(ws)

    assert not any(m["type"] == "tool_call" for m in msgs)


def test_ws_agent_plan_mode_allows_read_tools():
    """In plan mode, read_file is still forwarded as tool_call."""
    with TestClient(app) as client:
        _install_upstream(
            _stream_tool_call_handler(
                "read_file", "call-4", {"path": "b.py"}, "Here it is"
            )
        )
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({
                "type": "start_session",
                "model": "test-model",
                "permission_mode": "plan",
                "tools": ["read_file"],
            })
            ws.send_json({"type": "message", "content": "read b.py"})

            msg = ws.receive_json()
            assert msg["type"] == "tool_call"
            assert msg["name"] == "read_file"
            call_id = msg["id"]

            ws.send_json({"type": "tool_result", "id": call_id, "result": "content"})
            _drain_until_done(ws)


# ---------------------------------------------------------------------------
# Permission mode update
# ---------------------------------------------------------------------------


def test_ws_agent_update_permission_mode_sends_ack():
    with TestClient(app) as client:
        _install_upstream(_stream_handler(["ok"]))
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({"type": "start_session", "model": "test-model", "tools": []})

            ws.send_json({"type": "update_permission_mode", "mode": "auto"})

            ack = ws.receive_json()
            assert ack["type"] == "permission_mode_ack"
            assert ack["mode"] == "auto"


def test_ws_agent_permission_mode_update_affects_subsequent_turns():
    """Mode update to 'plan' mid-session blocks write tools on next turn."""
    # First turn: auto mode → write_file forwarded as tool_call
    # Second turn: after updating to plan → write_file blocked
    # We only test the second turn here for simplicity.
    with TestClient(app) as client:
        _install_upstream(
            _stream_tool_call_handler(
                "write_file", "call-5", {"path": "c.py", "content": "y"}, "done"
            )
        )
        with client.websocket_connect("/ws/agent", headers=AUTH) as ws:
            ws.send_json({
                "type": "start_session",
                "model": "test-model",
                "permission_mode": "auto",
                "tools": ["write_file"],
            })

            # Switch to plan mode
            ws.send_json({"type": "update_permission_mode", "mode": "plan"})
            ack = ws.receive_json()
            assert ack["type"] == "permission_mode_ack"

            # Now send a message — write_file should be blocked in plan mode
            ws.send_json({"type": "message", "content": "write c.py"})
            msgs = _drain_until_done(ws)

    assert not any(m["type"] == "tool_call" for m in msgs)
