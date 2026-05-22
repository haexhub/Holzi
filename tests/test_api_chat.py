import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import conversations, messages

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _assistant_oneshot(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": "claude-opus-4-7",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _install_upstream_responses(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Install a MockTransport on app.state.upstream and return the list of
    request bodies that were sent upstream (mutated as calls arrive)."""
    iter_resp = iter(responses)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        try:
            payload = next(iter_resp)
        except StopIteration as exc:
            raise AssertionError("upstream called more times than expected") from exc
        return httpx.Response(200, json=payload)

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )
    return seen


@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


def _parse_sse(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    """Parse a series of `event: name\\ndata: {...}\\n\\n` blocks."""
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
            events.append((event, data))
    return events


async def test_api_chat_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 401


async def test_api_chat_creates_new_conversation_when_none_provided(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("hello back")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    event_names = [name for name, _ in events]
    assert event_names == ["session", "text", "done"]

    session_evt = events[0][1]
    text_evt = events[1][1]
    assert isinstance(session_evt["conversation_id"], int)
    assert text_evt["content"] == "hello back"

    conv_id = session_evt["conversation_id"]
    convo = await conversations.get(app.state.db, conv_id)
    assert convo is not None
    assert convo.channel == "web"
    msgs = await messages.list_by_conversation(app.state.db, conv_id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"),
        ("assistant", "hello back"),
    ]


async def test_api_chat_continues_existing_conversation(
    client: httpx.AsyncClient,
) -> None:
    convo = await conversations.create(app.state.db, channel="web", ts=1000)
    _install_upstream_responses([_assistant_oneshot("ack")])

    async with client.stream(
        "POST",
        "/api/chat",
        headers=AUTH,
        json={"message": "round 2", "conversation_id": convo.id},
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = _parse_sse(body)
    assert events[0][1]["conversation_id"] == convo.id
    msgs = await messages.list_by_conversation(app.state.db, convo.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "round 2"),
        ("assistant", "ack"),
    ]


async def test_api_chat_unknown_conversation_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_upstream_responses([_assistant_oneshot("never reached")])
    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hi", "conversation_id": 99999},
    )
    assert response.status_code == 404


async def test_api_chat_rejects_non_web_conversation(
    client: httpx.AsyncClient,
) -> None:
    """Channel semantics: /api/chat must not inject web messages into Signal threads."""
    signal_convo = await conversations.create(app.state.db, channel="signal", ts=1000)
    _install_upstream_responses([_assistant_oneshot("never reached")])

    response = await client.post(
        "/api/chat",
        headers=AUTH,
        json={"message": "hijack", "conversation_id": signal_convo.id},
    )
    assert response.status_code == 400
    # Nothing should have been written into the signal conversation.
    msgs = await messages.list_by_conversation(app.state.db, signal_convo.id)
    assert msgs == []


async def test_api_chat_missing_message_returns_400(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/chat", headers=AUTH, json={})
    assert response.status_code == 400


async def test_api_chat_passes_tool_catalog_to_agent(
    client: httpx.AsyncClient,
) -> None:
    seen = _install_upstream_responses([_assistant_oneshot("done")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    # The agent should have included Hermes tools in the upstream request.
    sent = seen[0]
    tools = sent.get("tools")
    assert tools, "agent should pass tools to upstream"
    names = {t["function"]["name"] for t in tools}
    assert "recall_memory" in names
    assert "save_note" in names
    assert "todo_add" in names


async def test_api_chat_cross_channel_send_filters_web_target(
    client: httpx.AsyncClient,
) -> None:
    """cross_channel_send should refuse to write back to the channel the
    /api/chat request itself uses (recursion guard)."""
    # Make the agent call cross_channel_send(channel='web', ...) and observe
    # the error the tool returns in the second LLM round-trip.
    tool_call = {
        "id": "call_loop",
        "type": "function",
        "function": {
            "name": "cross_channel_send",
            "arguments": json.dumps({"channel": "web", "message": "hello me"}),
        },
    }
    first_resp = {
        "id": "x",
        "model": "claude-opus-4-7",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [tool_call],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    seen = _install_upstream_responses([first_resp, _assistant_oneshot("done")])

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "loop me"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
        assert response.status_code == 200

    # The tool result that came back to the LLM in the second iteration
    # should contain an error mentioning the current channel.
    second_req = seen[1]
    tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "web" in tool_msgs[0]["content"]
    assert "error" in tool_msgs[0]["content"].lower()
