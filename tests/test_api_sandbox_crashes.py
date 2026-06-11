"""End-to-end tests for GET /api/sandbox/crashes (Plan 20-A).

The endpoint reads the `sandbox_crashes` table populated by the
lifespan-registered persistence handler (Plan 20-A). These tests insert
rows directly through the repository so the endpoint contract — auth,
ordering, shape, limit clamping — is exercised without depending on a
real Podman backend.
"""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import sandbox_crashes as repo

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


async def test_sandbox_crashes_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/sandbox/crashes")
    assert response.status_code == 401


async def test_sandbox_crashes_empty(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/sandbox/crashes", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == []


async def test_sandbox_crashes_returns_documented_shape(
    client: httpx.AsyncClient,
) -> None:
    await repo.insert(
        app.state.db,
        workspace_id="ws-1",
        sandbox_id="cont-abc",
        crashed_at=1_000,
        state="crashed",
        exit_code=137,
    )
    body = (await client.get("/api/sandbox/crashes", headers=AUTH)).json()
    assert len(body) == 1
    only = body[0]
    assert set(only.keys()) == {
        "id",
        "workspace_id",
        "sandbox_id",
        "crashed_at",
        "state",
        "exit_code",
        "last_message",
    }
    assert only["workspace_id"] == "ws-1"
    assert only["sandbox_id"] == "cont-abc"
    assert only["crashed_at"] == 1_000
    assert only["state"] == "crashed"
    assert only["exit_code"] == 137
    assert only["last_message"] is None


async def test_sandbox_crashes_orders_newest_first(
    client: httpx.AsyncClient,
) -> None:
    for ts, sid in [(1_000, "cont-old"), (3_000, "cont-new"), (2_000, "cont-mid")]:
        await repo.insert(
            app.state.db,
            workspace_id="ws-1",
            sandbox_id=sid,
            crashed_at=ts,
            state="crashed",
            exit_code=1,
        )
    body = (await client.get("/api/sandbox/crashes", headers=AUTH)).json()
    assert [r["sandbox_id"] for r in body] == ["cont-new", "cont-mid", "cont-old"]


async def test_sandbox_crashes_respects_limit(client: httpx.AsyncClient) -> None:
    for ts in range(5):
        await repo.insert(
            app.state.db,
            workspace_id="ws-1",
            sandbox_id=f"cont-{ts}",
            crashed_at=ts,
            state="crashed",
            exit_code=1,
        )
    body = (await client.get("/api/sandbox/crashes?limit=2", headers=AUTH)).json()
    assert [r["sandbox_id"] for r in body] == ["cont-4", "cont-3"]


async def test_sandbox_crashes_limit_clamped_low(
    client: httpx.AsyncClient,
) -> None:
    """Query(ge=1) rejects 0 with 422 — protects against an accidental
    `?limit=0` returning an empty list and masking the real config bug."""
    response = await client.get("/api/sandbox/crashes?limit=0", headers=AUTH)
    assert response.status_code == 422


async def test_sandbox_crashes_limit_clamped_high(
    client: httpx.AsyncClient,
) -> None:
    """Query(le=100) — a runaway caller can't ask for the entire table."""
    response = await client.get(
        "/api/sandbox/crashes?limit=10000", headers=AUTH
    )
    assert response.status_code == 422


async def test_sandbox_crashes_carries_oom_and_null_exit_code(
    client: httpx.AsyncClient,
) -> None:
    """OOM transitions have no clean exit code — JSON must surface `null`,
    not a default 0, so the frontend can render '—' instead of misleading."""
    await repo.insert(
        app.state.db,
        workspace_id="ws-1",
        sandbox_id="cont-oom",
        crashed_at=1_000,
        state="oom",
        exit_code=None,
    )
    body = (await client.get("/api/sandbox/crashes", headers=AUTH)).json()
    assert body[0]["state"] == "oom"
    assert body[0]["exit_code"] is None
