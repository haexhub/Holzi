"""REST endpoints that expose the SandboxManager (Plan 11b-b).

The endpoints are reachable in any deployment, but only return data when a
sandbox manager is configured. Without one (the default in tests), they must
return 503 cleanly so the frontend can fall back to "no sandbox" instead of
spinning on a hung promise."""

import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.sandbox import (
    ResourceLimits,
    SandboxManager,
    SandboxState,
)
from hermes.sandbox.fake import FakeSandboxBackend

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


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


async def test_sandbox_status_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/workspaces/ws-1/sandbox")
    assert response.status_code == 401


async def test_sandbox_status_503_when_not_configured(
    client: httpx.AsyncClient,
) -> None:
    """The default test app has no sandbox manager — the endpoint must surface
    that as 503 so the frontend can render "sandbox unavailable" instead of
    waiting on a never-fulfilled handle."""
    assert app.state.sandbox_manager is None
    response = await client.get("/api/workspaces/ws-1/sandbox", headers=AUTH)
    assert response.status_code == 503


async def test_sandbox_restart_503_when_not_configured(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/workspaces/ws-1/sandbox/restart", headers=AUTH)
    assert response.status_code == 503


async def test_sandbox_status_absent_for_unspawned_workspace(
    client: httpx.AsyncClient,
) -> None:
    """With a manager but no cached handle for the workspace, state is
    `absent` — distinct from `removed` (which would mean a handle existed and
    is gone)."""
    backend = FakeSandboxBackend()
    app.state.sandbox_manager = SandboxManager(
        backend=backend,
        image="hermes-sandbox:test",
        network="none",
        default_limits=ResourceLimits(cpus=1.0, memory_mb=512, disk_mb=1024),
    )
    try:
        response = await client.get("/api/workspaces/ws-1/sandbox", headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {
            "workspace_id": "ws-1",
            "state": "absent",
            "exit_code": None,
        }
    finally:
        await app.state.sandbox_manager.shutdown()
        app.state.sandbox_manager = None


async def test_sandbox_restart_returns_running_status(
    client: httpx.AsyncClient,
) -> None:
    backend = FakeSandboxBackend()
    app.state.sandbox_manager = SandboxManager(
        backend=backend,
        image="hermes-sandbox:test",
        network="none",
        default_limits=ResourceLimits(cpus=1.0, memory_mb=512, disk_mb=1024),
    )
    try:
        # Spin up a workspace, crash it, then restart via the endpoint.
        handle = await app.state.sandbox_manager.get_workspace("ws-1")
        backend.simulate_crash(handle.id)

        response = await client.post(
            "/api/workspaces/ws-1/sandbox/restart", headers=AUTH
        )
        assert response.status_code == 200
        body = response.json()
        assert body["workspace_id"] == "ws-1"
        assert body["state"] == SandboxState.running.value
    finally:
        await app.state.sandbox_manager.shutdown()
        app.state.sandbox_manager = None


# --- SSE crash-handler pairing ----------------------------------------------


def _install_upstream_oneshot(content: str) -> None:
    """Mirror of `test_api_chat`'s helper — pinned here so this test file
    stays self-contained and we don't reach into another test module."""

    def _to_sse(payload: dict[str, Any]) -> bytes:
        msg = payload["choices"][0]["message"]
        out = b""
        if msg.get("content"):
            chunk = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": msg["content"]},
                        "finish_reason": None,
                    }
                ]
            }
            out += f"data: {json.dumps(chunk)}\n\n".encode()
        final = {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        out += f"data: {json.dumps(final)}\n\n".encode()
        out += b"data: [DONE]\n\n"
        return out

    body = _to_sse(
        {
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


async def test_chat_stream_subscribes_and_unsubscribes_crash_handler(
    client: httpx.AsyncClient,
) -> None:
    """Every open chat stream registers one crash handler and removes it on
    finish — otherwise per-request closures would accumulate forever.

    ASGITransport buffers the SSE response completely, so we can't assert
    "subscribed mid-flight" the obvious way. Instead we spy add/remove and
    assert each is called exactly once, with the same handler, and the
    handler list is empty after the stream concludes."""
    mgr = SandboxManager(
        backend=FakeSandboxBackend(),
        image="hermes-sandbox:test",
        network="none",
        default_limits=ResourceLimits(cpus=1.0, memory_mb=512, disk_mb=1024),
    )
    app.state.sandbox_manager = mgr

    added: list[Any] = []
    removed: list[Any] = []
    original_add = mgr.add_crash_handler
    original_remove = mgr.remove_crash_handler

    def spy_add(handler: Any) -> None:
        added.append(handler)
        original_add(handler)

    def spy_remove(handler: Any) -> None:
        removed.append(handler)
        original_remove(handler)

    mgr.add_crash_handler = spy_add  # type: ignore[method-assign]
    mgr.remove_crash_handler = spy_remove  # type: ignore[method-assign]

    try:
        _install_upstream_oneshot("hi back")

        async with client.stream(
            "POST", "/api/chat", headers=AUTH, json={"message": "hi"}
        ) as response:
            assert response.status_code == 200
            async for _ in response.aiter_bytes():
                pass

        assert len(added) == 1
        assert len(removed) == 1
        assert added[0] is removed[0]
        assert mgr._crash_handlers == []  # noqa: SLF001
    finally:
        await mgr.shutdown()
        app.state.sandbox_manager = None
