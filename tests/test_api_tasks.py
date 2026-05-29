import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import agent_tasks

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


# ---------------------------------------------------------------------------
# auth + listing
# ---------------------------------------------------------------------------


async def test_api_tasks_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/tasks")
    assert response.status_code == 401


async def test_api_tasks_list_returns_nearest_due_first(
    client: httpx.AsyncClient,
) -> None:
    later = await agent_tasks.create(
        app.state.db, title="later", prompt="x", due_at=2_000_000_000
    )
    earlier = await agent_tasks.create(
        app.state.db, title="earlier", prompt="x", due_at=1_700_000_000
    )

    response = await client.get("/api/tasks", headers=AUTH)
    assert response.status_code == 200
    ids = [t["id"] for t in response.json()]
    assert ids == [earlier.id, later.id]


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_api_tasks_create_one_shot(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/tasks",
        headers=AUTH,
        json={
            "title": "wake up",
            "prompt": "say good morning",
            "due_at": 2_000_000_000,
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "wake up"
    assert data["due_at"] == 2_000_000_000
    assert data["schedule"] is None
    assert data["enabled"] is True
    assert data["last_status"] is None


async def test_api_tasks_create_recurring(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/tasks",
        headers=AUTH,
        json={
            "title": "daily summary",
            "prompt": "summarise the day",
            "schedule": "0 8 * * *",
            "timezone": "UTC",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["schedule"] == "0 8 * * *"
    # The repo materialises the first firing into due_at.
    assert data["due_at"] is not None
    assert data["enabled"] is True


async def test_api_tasks_create_rejects_neither_set(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/tasks",
        headers=AUTH,
        json={"title": "x", "prompt": "y"},
    )
    assert response.status_code == 400


async def test_api_tasks_create_rejects_both_set(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/tasks",
        headers=AUTH,
        json={
            "title": "x",
            "prompt": "y",
            "due_at": 2_000_000_000,
            "schedule": "0 8 * * *",
        },
    )
    assert response.status_code == 400


async def test_api_tasks_create_rejects_invalid_cron(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/tasks",
        headers=AUTH,
        json={"title": "x", "prompt": "y", "schedule": "not-a-cron"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


async def test_api_tasks_patch_pauses_and_resumes(
    client: httpx.AsyncClient,
) -> None:
    task = await agent_tasks.create(
        app.state.db, title="t", prompt="x", due_at=2_000_000_000
    )

    paused = await client.patch(
        f"/api/tasks/{task.id}", headers=AUTH, json={"enabled": False}
    )
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False

    resumed = await client.patch(
        f"/api/tasks/{task.id}", headers=AUTH, json={"enabled": True}
    )
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True


async def test_api_tasks_patch_updates_prompt_and_title(
    client: httpx.AsyncClient,
) -> None:
    task = await agent_tasks.create(
        app.state.db, title="old", prompt="old prompt", due_at=2_000_000_000
    )
    response = await client.patch(
        f"/api/tasks/{task.id}",
        headers=AUTH,
        json={"title": "new", "prompt": "new prompt"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "new"
    assert data["prompt"] == "new prompt"


async def test_api_tasks_patch_switches_one_shot_to_recurring(
    client: httpx.AsyncClient,
) -> None:
    task = await agent_tasks.create(
        app.state.db, title="t", prompt="x", due_at=2_000_000_000
    )
    response = await client.patch(
        f"/api/tasks/{task.id}",
        headers=AUTH,
        json={"clear_due_at": True, "schedule": "0 8 * * *"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["schedule"] == "0 8 * * *"
    # The repo recomputed due_at off the cron expression.
    assert data["due_at"] is not None


async def test_api_tasks_patch_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.patch(
        "/api/tasks/99999", headers=AUTH, json={"title": "x"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_api_tasks_delete_removes(client: httpx.AsyncClient) -> None:
    task = await agent_tasks.create(
        app.state.db, title="t", prompt="x", due_at=2_000_000_000
    )
    response = await client.delete(f"/api/tasks/{task.id}", headers=AUTH)
    assert response.status_code == 204
    assert await agent_tasks.get(app.state.db, task.id) is None


async def test_api_tasks_delete_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/tasks/99999", headers=AUTH)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# run-now
# ---------------------------------------------------------------------------


async def test_api_tasks_run_now_returns_queued(
    client: httpx.AsyncClient, monkeypatch,
) -> None:
    task = await agent_tasks.create(
        app.state.db, title="t", prompt="x", due_at=2_000_000_000
    )

    # Stub scheduler.run_now so we don't actually drive a full agent
    # turn through the upstream — we only want the route's contract.
    called: list[int] = []

    async def fake_run_now(task_id: int) -> str:
        called.append(task_id)
        return "ok"

    monkeypatch.setattr(app.state.scheduler, "run_now", fake_run_now)

    response = await client.post(
        f"/api/tasks/{task.id}/run", headers=AUTH
    )
    assert response.status_code == 202
    data = response.json()
    assert data["task_id"] == task.id
    assert data["status"] == "queued"

    # Background task fires asynchronously — wait briefly for it to land.
    import asyncio
    for _ in range(20):
        if called:
            break
        await asyncio.sleep(0.01)
    assert called == [task.id]


async def test_api_tasks_run_now_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/tasks/99999/run", headers=AUTH)
    assert response.status_code == 404


async def test_api_tasks_rejects_invalid_limit(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/tasks?limit=-1", headers=AUTH)
    assert response.status_code == 400
