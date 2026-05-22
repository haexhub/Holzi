import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import todos

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


async def test_api_todos_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/todos")
    assert response.status_code == 401


async def test_api_todos_list_returns_open_by_default(
    client: httpx.AsyncClient,
) -> None:
    t1 = await todos.add(app.state.db, content="open")
    t2 = await todos.add(app.state.db, content="closed")
    await todos.mark_done(app.state.db, t2.id)

    response = await client.get("/api/todos", headers=AUTH)
    assert response.status_code == 200
    items = response.json()
    assert [t["id"] for t in items] == [t1.id]


async def test_api_todos_list_only_open_false_returns_all(
    client: httpx.AsyncClient,
) -> None:
    t1 = await todos.add(app.state.db, content="open")
    t2 = await todos.add(app.state.db, content="closed")
    await todos.mark_done(app.state.db, t2.id)

    response = await client.get("/api/todos?only_open=false", headers=AUTH)
    assert response.status_code == 200
    ids = {t["id"] for t in response.json()}
    assert ids == {t1.id, t2.id}


async def test_api_todos_post_creates(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/todos",
        headers=AUTH,
        json={"content": "buy milk", "tags": ["groceries", "today"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "buy milk"
    assert data["tags"] == "groceries,today"
    assert data["done_at"] is None


async def test_api_todos_patch_marks_done(client: httpx.AsyncClient) -> None:
    t = await todos.add(app.state.db, content="finish me")
    response = await client.patch(
        f"/api/todos/{t.id}", headers=AUTH, json={"done": True}
    )
    assert response.status_code == 200
    assert response.json()["done_at"] is not None

    stored = await todos.get(app.state.db, t.id)
    assert stored is not None
    assert stored.done_at is not None


async def test_api_todos_patch_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.patch(
        "/api/todos/99999", headers=AUTH, json={"done": True}
    )
    assert response.status_code == 404


async def test_api_todos_delete_removes(client: httpx.AsyncClient) -> None:
    t = await todos.add(app.state.db, content="bye")
    response = await client.delete(f"/api/todos/{t.id}", headers=AUTH)
    assert response.status_code == 204
    assert await todos.get(app.state.db, t.id) is None


async def test_api_todos_delete_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/todos/99999", headers=AUTH)
    assert response.status_code == 404


async def test_api_todos_rejects_invalid_limit(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/todos?limit=-1", headers=AUTH)
    assert response.status_code == 400
