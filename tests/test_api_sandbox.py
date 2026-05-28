"""REST endpoints that expose the SandboxManager (Plan 11b-b).

The endpoints are reachable in any deployment, but only return data when a
sandbox manager is configured. Without one (the default in tests), they must
return 503 cleanly so the frontend can fall back to "no sandbox" instead of
spinning on a hung promise."""

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
async def client():
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
