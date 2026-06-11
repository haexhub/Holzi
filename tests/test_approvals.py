"""Plan 21: approval-granularity tests.

The four-decision protocol (`allow_once`, `allow_session`, `allow_always`,
`deny`) plus the standing-approval lists (`session_approvals` on app.state +
`tool_approvals` table) get covered here in three layers:

1. Repository unit tests — `grant_always` / `is_always_allowed` /
   `revoke_always` / `list_always` against a fresh per-test SQLite DB.
2. Endpoint tests — `GET /api/approvals/standing`, `DELETE .../{tool}`, and
   the four-decision `POST /api/approvals/{id}` body validation.
3. Integration tests — `/api/chat` with mcp_install (the only remaining
   approval-gated built-in after Plan 34 removed the messenger surface),
   asserting that a pre-seeded standing approval skips the
   `approval_required` event entirely and that a `deny` with reason flows
   into the tool error fed back to the LLM. mcp_install is called with
   args that fail validation early, so no real MCP server gets installed
   in the test DB.
"""
import asyncio
import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.agent import ApprovalDecision
from hermes.main import app
from hermes.repository import approvals as approvals_repo
from hermes.repository import conversations

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


# ---------------------------------------------------------------------------
# Shared helpers (mirror the ones in test_api_chat.py — keep the surface
# small and deliberate so this file stays self-contained).
# ---------------------------------------------------------------------------


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
        body = _to_sse_stream(payload)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://fake-proxy",
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
    """Resolve the first future that lands on app.state.approvals.

    ASGITransport buffers the whole SSE body before returning so a real
    mid-stream POST /api/approvals isn't testable (same constraint
    test_api_chat.py documents); resolving the future from a sibling task
    exercises the same pause→resume path.
    """
    for _ in range(500):
        for approval_id, future in list(app.state.approvals.items()):
            if not future.done():
                future.set_result(
                    ApprovalDecision(decision=decision, reason=reason)  # type: ignore[arg-type]
                )
                return approval_id
        await asyncio.sleep(0.01)
    raise AssertionError("no approval became pending")


@pytest.fixture
async def client(pg_db):
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


# ---------------------------------------------------------------------------
# Repository unit tests
# ---------------------------------------------------------------------------


async def test_repo_grant_and_list_always(conn) -> None:
    """`grant_always` upserts and `list_always` returns granted_at/last_used_at."""
    await approvals_repo.grant_always(conn, "mcp_install", now=1000)
    rows = await approvals_repo.list_always(conn)
    assert [(r.tool_name, r.granted_at) for r in rows] == [
        ("mcp_install", 1000)
    ]
    # last_used_at is None on a fresh grant.
    assert rows[0].last_used_at is None


async def test_repo_grant_always_is_idempotent(conn) -> None:
    """Re-granting the same tool refreshes `granted_at` without duplicate rows."""
    await approvals_repo.grant_always(conn, "mcp_install", now=1000)
    await approvals_repo.grant_always(conn, "mcp_install", now=2000)
    rows = await approvals_repo.list_always(conn)
    assert len(rows) == 1
    assert rows[0].granted_at == 2000


async def test_repo_is_always_allowed(conn) -> None:
    assert (
        await approvals_repo.is_always_allowed(conn, "mcp_install")
    ) is False
    await approvals_repo.grant_always(conn, "mcp_install", now=1000)
    assert (
        await approvals_repo.is_always_allowed(conn, "mcp_install")
    ) is True


async def test_repo_revoke_always(conn) -> None:
    await approvals_repo.grant_always(conn, "mcp_install", now=1000)
    removed = await approvals_repo.revoke_always(conn, "mcp_install")
    assert removed is True
    assert (
        await approvals_repo.is_always_allowed(conn, "mcp_install")
    ) is False
    # Revoking a missing row returns False (caller surfaces as 404).
    assert (await approvals_repo.revoke_always(conn, "mcp_install")) is False


# ---------------------------------------------------------------------------
# POST /api/approvals/{id} body validation (four decisions + reason cap)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision",
    ["allow_once", "allow_session", "allow_always", "deny"],
)
async def test_post_approval_accepts_four_decisions(
    client: httpx.AsyncClient, decision: str
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    app.state.approvals["unit-decision"] = future
    try:
        resp = await client.post(
            "/api/approvals/unit-decision",
            headers=AUTH,
            json={"decision": decision},
        )
        assert resp.status_code == 204
        assert future.done()
        assert future.result().decision == decision
    finally:
        app.state.approvals.pop("unit-decision", None)


async def test_post_approval_rejects_unknown_decision(
    client: httpx.AsyncClient,
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    app.state.approvals["unit-bad"] = future
    try:
        resp = await client.post(
            "/api/approvals/unit-bad",
            headers=AUTH,
            json={"decision": "maybe"},
        )
        assert resp.status_code == 422
        assert not future.done()
    finally:
        app.state.approvals.pop("unit-bad", None)


async def test_post_approval_reason_cap_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """Reason longer than 500 characters is rejected by the request model."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[ApprovalDecision] = loop.create_future()
    app.state.approvals["unit-toolong"] = future
    try:
        resp = await client.post(
            "/api/approvals/unit-toolong",
            headers=AUTH,
            json={"decision": "deny", "reason": "x" * 501},
        )
        assert resp.status_code == 422
        assert not future.done()
    finally:
        app.state.approvals.pop("unit-toolong", None)


# ---------------------------------------------------------------------------
# GET /api/approvals/standing + DELETE /api/approvals/standing/{tool}
# ---------------------------------------------------------------------------


async def test_get_standing_returns_always_and_session(
    client: httpx.AsyncClient,
) -> None:
    # Seed: an always-row directly via the repo, plus a session entry in state.
    await approvals_repo.grant_always(app.state.db, "mcp_install", now=1000)
    app.state.session_approvals[42] = {"save_note"}
    try:
        resp = await client.get("/api/approvals/standing", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["always"] == [
            {
                "tool": "mcp_install",
                "granted_at": 1000,
                "last_used_at": None,
            }
        ]
        # Session entries serialise as (conversation_id, tool) pairs.
        assert {(s["conversation_id"], s["tool"]) for s in body["session"]} == {
            (42, "save_note")
        }
    finally:
        await approvals_repo.revoke_always(app.state.db, "mcp_install")
        app.state.session_approvals.pop(42, None)


async def test_get_standing_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/approvals/standing")
    assert resp.status_code == 401


async def test_delete_standing_always_removes_row(
    client: httpx.AsyncClient,
) -> None:
    await approvals_repo.grant_always(app.state.db, "mcp_install", now=1000)
    resp = await client.delete(
        "/api/approvals/standing/mcp_install?scope=always",
        headers=AUTH,
    )
    assert resp.status_code == 204
    assert (
        await approvals_repo.is_always_allowed(app.state.db, "mcp_install")
    ) is False


async def test_delete_standing_always_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.delete(
        "/api/approvals/standing/mcp_install?scope=always",
        headers=AUTH,
    )
    assert resp.status_code == 404


async def test_delete_standing_session_removes_entry(
    client: httpx.AsyncClient,
) -> None:
    app.state.session_approvals[7] = {"save_note", "mcp_install"}
    try:
        resp = await client.delete(
            "/api/approvals/standing/save_note?scope=session",
            headers=AUTH,
        )
        assert resp.status_code == 204
        # Only the targeted tool is gone — the other session entry survives.
        assert app.state.session_approvals[7] == {"mcp_install"}
    finally:
        app.state.session_approvals.pop(7, None)


async def test_delete_standing_rejects_unknown_scope(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.delete(
        "/api/approvals/standing/x?scope=forever",
        headers=AUTH,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Integration with /api/chat — pre-seeded standing approvals skip the gate.
# ---------------------------------------------------------------------------


async def test_chat_skips_approval_when_always_allowed(
    client: httpx.AsyncClient,
) -> None:
    """A persisted always-grant pre-empts the approval gate: no
    `approval_required` event is emitted and the tool runs immediately."""
    await approvals_repo.grant_always(app.state.db, "mcp_install", now=1000)
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

    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "go"}
    ) as response:
        assert response.status_code == 200
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk

    names = [name for name, _ in _parse_sse(body)]
    assert "approval_required" not in names
    assert "tool_call" in names
    assert names[-1] == "done"


async def test_chat_skips_approval_when_session_allowed(
    client: httpx.AsyncClient,
) -> None:
    """An in-memory session grant for `(conversation_id, tool_name)`
    pre-empts the gate. New chats default to the existing conversation
    (none yet) — we create one explicitly and seed the session entry under
    its id."""
    convo = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    app.state.session_approvals[convo.id] = {"mcp_install"}
    try:
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
        async with client.stream(
            "POST",
            "/api/chat",
            headers=AUTH,
            json={"message": "go", "conversation_id": convo.id},
        ) as response:
            assert response.status_code == 200
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk

        names = [name for name, _ in _parse_sse(body)]
        assert "approval_required" not in names
        assert "tool_call" in names
    finally:
        app.state.session_approvals.pop(convo.id, None)


async def test_chat_session_grant_does_not_carry_to_new_conversation(
    client: httpx.AsyncClient,
) -> None:
    """Session grants are conversation-scoped. Seeding `convo_a` and chatting
    on `convo_b` still requires approval on `convo_b`."""
    convo_a = await conversations.create(app.state.db, user_id=1, channel="web", ts=1000)
    convo_b = await conversations.create(app.state.db, user_id=1, channel="web", ts=1001)
    app.state.session_approvals[convo_a.id] = {"mcp_install"}
    try:
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
        resolver = asyncio.create_task(
            _resolve_first_pending_approval("allow_once")
        )
        async with client.stream(
            "POST",
            "/api/chat",
            headers=AUTH,
            json={"message": "go", "conversation_id": convo_b.id},
        ) as response:
            assert response.status_code == 200
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
        await resolver

        names = [name for name, _ in _parse_sse(body)]
        assert "approval_required" in names
    finally:
        app.state.session_approvals.pop(convo_a.id, None)


async def test_chat_allow_session_persists_session_grant(
    client: httpx.AsyncClient,
) -> None:
    """When the user resolves an approval with `allow_session`, the wrapper
    in routes/api.py adds `(conversation_id, tool_name)` to
    `app.state.session_approvals` so the next call in the same chat
    bypasses the gate."""
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
    resolver = asyncio.create_task(
        _resolve_first_pending_approval("allow_session")
    )
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "go"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    events = _parse_sse(body)
    session_evt = next(d for n, d in events if n == "session")
    conv_id = session_evt["conversation_id"]
    assert "mcp_install" in app.state.session_approvals.get(conv_id, set())


async def test_chat_allow_always_persists_to_db(
    client: httpx.AsyncClient,
) -> None:
    """`allow_always` writes a `tool_approvals` row that survives a fresh
    `app.state.session_approvals`. We assert the DB row directly — surviving
    a real process restart would just reload the same row at boot."""
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
    resolver = asyncio.create_task(
        _resolve_first_pending_approval("allow_always")
    )
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "go"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    assert (
        await approvals_repo.is_always_allowed(
            app.state.db, "mcp_install"
        )
    ) is True
    # Simulate a restart: clear the in-memory session map.
    app.state.session_approvals.clear()
    # The persisted always-grant still pre-empts the gate.
    assert (
        await approvals_repo.is_always_allowed(
            app.state.db, "mcp_install"
        )
    ) is True


async def test_chat_deny_reason_flows_into_tool_error(
    client: httpx.AsyncClient,
) -> None:
    """A `deny` with a reason ends up in the tool message the LLM sees on
    its next round, so it can self-correct."""
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
    resolver = asyncio.create_task(
        _resolve_first_pending_approval("deny", reason="this would page oncall")
    )
    async with client.stream(
        "POST", "/api/chat", headers=AUTH, json={"message": "go"}
    ) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
    await resolver

    # Second upstream round saw the denied tool result.
    second_req = seen[1]
    tool_msgs = [m for m in second_req["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "this would page oncall" in tool_msgs[0]["content"]
