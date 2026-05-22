import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import notes

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


async def test_api_notes_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/notes")
    assert response.status_code == 401


async def test_api_notes_list_returns_all(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, key="a", content="alpha", tags="x")
    await notes.upsert(app.state.db, key="b", content="beta", tags="y")

    response = await client.get("/api/notes", headers=AUTH)
    assert response.status_code == 200
    keys = sorted(n["key"] for n in response.json())
    assert keys == ["a", "b"]


async def test_api_notes_get_returns_single(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, key="foo.bar", content="baz")

    response = await client.get("/api/notes/foo.bar", headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert data["key"] == "foo.bar"
    assert data["content"] == "baz"


async def test_api_notes_get_missing_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/notes/nope", headers=AUTH)
    assert response.status_code == 404


async def test_api_notes_post_creates(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/notes",
        headers=AUTH,
        json={"key": "k", "content": "v", "tags": ["t1", "t2"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["key"] == "k"
    assert data["content"] == "v"
    assert data["tags"] == "t1,t2"

    stored = await notes.get(app.state.db, "k")
    assert stored is not None
    assert stored.content == "v"


async def test_api_notes_post_missing_field_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/notes", headers=AUTH, json={"key": "x"})
    assert response.status_code == 422


async def test_api_notes_put_updates(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, key="k", content="old")
    response = await client.put(
        "/api/notes/k", headers=AUTH, json={"content": "new"}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "new"

    stored = await notes.get(app.state.db, "k")
    assert stored is not None
    assert stored.content == "new"


async def test_api_notes_delete_removes(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, key="kill", content="me")
    response = await client.delete("/api/notes/kill", headers=AUTH)
    assert response.status_code == 204
    assert await notes.get(app.state.db, "kill") is None


async def test_api_notes_delete_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/notes/missing", headers=AUTH)
    assert response.status_code == 404
