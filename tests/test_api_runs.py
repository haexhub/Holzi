"""End-to-end tests for the agent_runs persistence layer and GET /api/runs.

The agent_runs table is the persistent source of truth for chat history;
the in-memory `app.state.chat_runs` registry from Plan 03 is a thin index
over the currently-active subset. Each /api/chat turn (regardless of
channel) writes exactly one row through the run-tracker context manager:
status starts as "running", transitions to one of success / cancelled /
error in the finally block, with timing and (where the upstream provides
it) token-usage metrics filled in.
"""
import asyncio
import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import conversations, runs

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


def _to_sse_stream(
    payload: dict[str, Any], usage: dict[str, int] | None = None
) -> bytes:
    """Turn a non-streaming canned response into an OpenAI-style SSE body.

    When ``usage`` is given, an extra terminal chunk with an empty choices
    list and a populated ``usage`` block is emitted right before ``[DONE]``
    — mirrors what OpenAI sends when stream_options.include_usage is on.
    """
    msg = payload["choices"][0]["message"]
    content = msg.get("content")
    out = b""
    if content:
        chunk = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
            ]
        }
        out += f"data: {json.dumps(chunk)}\n\n".encode()
    if usage is not None:
        usage_chunk = {"choices": [], "usage": usage}
        out += f"data: {json.dumps(usage_chunk)}\n\n".encode()
    out += b"data: [DONE]\n\n"
    return out


def _install_oneshot_upstream(
    content: str, *, usage: dict[str, int] | None = None
) -> None:
    body = _to_sse_stream(_assistant_oneshot(content), usage=usage)

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
            events.append((event, data))
    return events


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


# ---------------------------------------------------------------------------
# /api/runs endpoint contract
# ---------------------------------------------------------------------------


async def test_api_runs_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/runs")
    assert response.status_code == 401


async def test_api_runs_lists_empty_when_no_runs(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/runs", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == []


async def test_api_runs_validates_limit(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/runs?limit=0", headers=AUTH)
    assert response.status_code == 400
    response = await client.get("/api/runs?limit=10000", headers=AUTH)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Success path persists a row with status=success + finished_at + tokens.
# ---------------------------------------------------------------------------


async def test_api_chat_writes_success_run_row(client: httpx.AsyncClient) -> None:
    _install_oneshot_upstream(
        "hello", usage={"prompt_tokens": 42, "completion_tokens": 7}
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = dict(_parse_sse(body))
    run_id = events["run"]["run_id"]
    conv_id = events["session"]["conversation_id"]

    row = await runs.get(app.state.db, run_id)
    assert row is not None
    assert row.id == run_id
    assert row.conversation_id == conv_id
    assert row.channel == "web"
    assert row.status == "success"
    assert row.started_at > 0
    assert row.finished_at is not None and row.finished_at >= row.started_at
    assert row.error_code is None
    assert row.error_message is None
    assert row.error_trace is None
    assert row.input_tokens == 42
    assert row.output_tokens == 7


async def test_api_chat_writes_success_row_without_usage(
    client: httpx.AsyncClient,
) -> None:
    """Tokens stay NULL when the upstream stream doesn't carry a usage block."""
    _install_oneshot_upstream("ok")

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    run_id = dict(_parse_sse(body))["run"]["run_id"]
    row = await runs.get(app.state.db, run_id)
    assert row is not None
    assert row.status == "success"
    assert row.input_tokens is None
    assert row.output_tokens is None


# ---------------------------------------------------------------------------
# Error path persists a row with status=error and full diagnostic context.
# ---------------------------------------------------------------------------


async def test_api_chat_writes_error_row_on_upstream_timeout(
    client: httpx.AsyncClient,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream too slow")

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    run_id = dict(_parse_sse(body))["run"]["run_id"]
    row = await runs.get(app.state.db, run_id)
    assert row is not None
    assert row.status == "error"
    assert row.error_code == "upstream_timeout"
    assert row.error_message and "too slow" in row.error_message
    assert row.error_trace and "ReadTimeout" in row.error_trace
    assert row.finished_at is not None


async def test_api_chat_writes_error_row_on_upstream_unreachable(
    client: httpx.AsyncClient,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    run_id = dict(_parse_sse(body))["run"]["run_id"]
    row = await runs.get(app.state.db, run_id)
    assert row is not None
    assert row.status == "error"
    assert row.error_code == "upstream_unreachable"


# ---------------------------------------------------------------------------
# Cancel path: ChatRunCancelled finalises the row as status=cancelled.
# ---------------------------------------------------------------------------


async def test_api_chat_writes_cancelled_row_when_event_set(
    client: httpx.AsyncClient,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # Flip every registered run before responding — same trick the
        # existing test_api_chat cancel test uses to work around
        # ASGITransport's full-buffering of SSE responses.
        for _, evt in list(app.state.chat_runs.items()):
            evt.set()
        body = _to_sse_stream(_assistant_oneshot("partial"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
    )

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    events = dict(_parse_sse(body))
    assert "cancelled" in events
    run_id = events["run"]["run_id"]
    row = await runs.get(app.state.db, run_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.finished_at is not None
    assert row.error_code is None


# ---------------------------------------------------------------------------
# Listing endpoint: filters and pagination.
# ---------------------------------------------------------------------------


async def test_api_runs_filters_by_conversation_id(
    client: httpx.AsyncClient,
) -> None:
    _install_oneshot_upstream("a")
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi a"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    conv_a = dict(_parse_sse(body))["session"]["conversation_id"]

    _install_oneshot_upstream("b")
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi b"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    conv_b = dict(_parse_sse(body))["session"]["conversation_id"]

    assert conv_a != conv_b

    listed_a = (
        await client.get(f"/api/runs?conversation_id={conv_a}", headers=AUTH)
    ).json()
    assert len(listed_a) == 1
    assert listed_a[0]["conversation_id"] == conv_a

    listed_all = (await client.get("/api/runs", headers=AUTH)).json()
    assert {r["conversation_id"] for r in listed_all} == {conv_a, conv_b}


async def test_api_runs_filters_by_status(client: httpx.AsyncClient) -> None:
    # One success, one error.
    _install_oneshot_upstream("ok")
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    def err_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom")

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(err_handler),
        base_url="http://fake-proxy",
    )
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    errors = (await client.get("/api/runs?status=error", headers=AUTH)).json()
    successes = (await client.get("/api/runs?status=success", headers=AUTH)).json()
    assert len(errors) == 1 and errors[0]["status"] == "error"
    assert len(successes) == 1 and successes[0]["status"] == "success"


async def test_api_runs_rejects_unknown_status(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/runs?status=bogus", headers=AUTH)
    assert response.status_code == 400


async def test_api_runs_paginates_with_limit(client: httpx.AsyncClient) -> None:
    # Drive three quick runs back-to-back.
    for i in range(3):
        _install_oneshot_upstream(f"r{i}")
        async with client.stream(
            "POST", "/api/chat", headers=AUTH, json={"message": f"hi {i}"}
        ) as response:
            async for _ in response.aiter_bytes():
                pass

    listed = (await client.get("/api/runs?limit=2", headers=AUTH)).json()
    assert len(listed) == 2
    # Newest-first ordering: started_at descending.
    ts = [row["started_at"] for row in listed]
    assert ts == sorted(ts, reverse=True)


async def test_api_runs_returns_full_run_shape(client: httpx.AsyncClient) -> None:
    """Smoke-check that the response shape is the public AgentRun model."""
    _install_oneshot_upstream("hello", usage={"prompt_tokens": 5, "completion_tokens": 9})
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    listed = (await client.get("/api/runs", headers=AUTH)).json()
    assert len(listed) == 1
    row = listed[0]
    assert set(row.keys()) == {
        "id",
        "conversation_id",
        "channel",
        "model",
        "started_at",
        "finished_at",
        "status",
        "error_code",
        "error_message",
        "error_trace",
        "input_tokens",
        "output_tokens",
    }
    assert row["status"] == "success"
    assert row["channel"] == "web"
    assert row["input_tokens"] == 5
    assert row["output_tokens"] == 9


# ---------------------------------------------------------------------------
# Direct repository tests — independent of the HTTP layer.
# ---------------------------------------------------------------------------


async def test_runs_repository_insert_and_finalize(conn) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    await runs.insert(
        conn,
        run_id="r1",
        conversation_id=convo.id,
        channel="web",
        model="m",
        started_at=1000,
    )
    row = await runs.get(conn, "r1")
    assert row is not None
    assert row.status == "running"
    assert row.finished_at is None

    await runs.finalize(
        conn,
        "r1",
        status="success",
        finished_at=1100,
        input_tokens=5,
        output_tokens=7,
    )
    row = await runs.get(conn, "r1")
    assert row is not None
    assert row.status == "success"
    assert row.finished_at == 1100
    assert row.input_tokens == 5
    assert row.output_tokens == 7


async def test_runs_repository_finalize_with_error(conn) -> None:
    convo = await conversations.create(conn, channel="web", ts=1000)
    await runs.insert(
        conn,
        run_id="r1",
        conversation_id=convo.id,
        channel="web",
        model="m",
        started_at=1000,
    )
    await runs.finalize(
        conn,
        "r1",
        status="error",
        finished_at=1050,
        error_code="upstream_timeout",
        error_message="too slow",
        error_trace="Traceback...",
    )
    row = await runs.get(conn, "r1")
    assert row is not None
    assert row.status == "error"
    assert row.error_code == "upstream_timeout"
    assert row.error_message == "too slow"
    assert row.error_trace == "Traceback..."


async def test_runs_finalize_does_not_clobber_terminal_row(conn) -> None:
    """A second finalize() must not overwrite an already-terminal row —
    the status='running' guard makes it a no-op."""
    convo = await conversations.create(conn, channel="web", ts=1000)
    await runs.insert(
        conn,
        run_id="r1",
        conversation_id=convo.id,
        channel="web",
        model="m",
        started_at=1000,
    )
    await runs.finalize(conn, "r1", status="success", finished_at=1100)
    # Late/duplicate finalize (e.g. a racing cancel path) must be ignored.
    await runs.finalize(
        conn,
        "r1",
        status="error",
        finished_at=2000,
        error_code="agent_error",
        error_message="should not stick",
    )
    row = await runs.get(conn, "r1")
    assert row is not None
    assert row.status == "success"
    assert row.finished_at == 1100
    assert row.error_code is None


async def test_runs_repository_list_filters(conn) -> None:
    c1 = await conversations.create(conn, channel="web", ts=1000)
    c2 = await conversations.create(conn, channel="signal", ts=1000)
    for i, (cid, status, channel) in enumerate(
        [
            (c1.id, "success", "web"),
            (c1.id, "error", "web"),
            (c2.id, "success", "signal"),
        ]
    ):
        rid = f"r{i}"
        await runs.insert(
            conn,
            run_id=rid,
            conversation_id=cid,
            channel=channel,
            model="m",
            started_at=1000 + i,
        )
        await runs.finalize(conn, rid, status=status, finished_at=1100 + i)

    all_rows = await runs.list_runs(conn)
    assert {r.id for r in all_rows} == {"r0", "r1", "r2"}

    only_c1 = await runs.list_runs(conn, conversation_id=c1.id)
    assert {r.id for r in only_c1} == {"r0", "r1"}

    only_errors = await runs.list_runs(conn, status="error")
    assert {r.id for r in only_errors} == {"r1"}

    page = await runs.list_runs(conn, limit=2)
    assert len(page) == 2
    # Newest-first.
    assert page[0].started_at >= page[1].started_at


# ---------------------------------------------------------------------------
# Cancel via the in-flight registry also persists to DB.
# ---------------------------------------------------------------------------


async def test_cancel_endpoint_flips_event_and_run_finalises_to_cancelled(
    client: httpx.AsyncClient,
) -> None:
    """Direct cancel-endpoint path: the endpoint itself returns 204 once the
    event is set; the row only transitions to ``cancelled`` once the agent
    task observes the event and run_agent's tracker finalises it. Without
    a real in-flight run, hitting cancel for a registry entry should still
    flip the event but not invent a row that never existed."""
    evt = asyncio.Event()
    app.state.chat_runs["never-existed"] = evt
    try:
        resp = await client.post(
            "/api/chat/runs/never-existed/cancel", headers=AUTH
        )
        assert resp.status_code == 204
        assert evt.is_set()
        # No agent_runs row was ever inserted for this fabricated run.
        assert await runs.get(app.state.db, "never-existed") is None
    finally:
        app.state.chat_runs.pop("never-existed", None)
