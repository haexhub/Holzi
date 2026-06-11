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
    await notes.upsert(app.state.db, user_id=1, key="a", content="alpha", tags="x")
    await notes.upsert(app.state.db, user_id=1, key="b", content="beta", tags="y")

    response = await client.get("/api/notes", headers=AUTH)
    assert response.status_code == 200
    keys = sorted(n["key"] for n in response.json())
    assert keys == ["a", "b"]


async def test_api_notes_get_returns_single(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, user_id=1, key="foo.bar", content="baz")

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

    stored = await notes.get(app.state.db, "k", user_id=1)
    assert stored is not None
    assert stored.content == "v"


async def test_api_notes_post_missing_field_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/notes", headers=AUTH, json={"key": "x"})
    assert response.status_code == 422


async def test_api_notes_put_updates(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, user_id=1, key="k", content="old")
    response = await client.put(
        "/api/notes/k", headers=AUTH, json={"content": "new"}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "new"

    stored = await notes.get(app.state.db, "k", user_id=1)
    assert stored is not None
    assert stored.content == "new"


async def test_api_notes_delete_removes(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, user_id=1, key="kill", content="me")
    response = await client.delete("/api/notes/kill", headers=AUTH)
    assert response.status_code == 204
    assert await notes.get(app.state.db, "kill", user_id=1) is None


async def test_api_notes_delete_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/notes/missing", headers=AUTH)
    assert response.status_code == 404


async def test_api_notes_rejects_invalid_limit(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/notes?limit=-1", headers=AUTH)
    assert response.status_code == 400


async def test_api_notes_search_filters_by_query(client: httpx.AsyncClient) -> None:
    await notes.upsert(app.state.db, user_id=1, key="a", content="reschedule the standup")
    await notes.upsert(app.state.db, user_id=1, key="b", content="buy milk")
    await notes.upsert(app.state.db, user_id=1, key="c", content="cancel the standup")

    response = await client.get("/api/notes?q=standup", headers=AUTH)
    assert response.status_code == 200
    keys = sorted(n["key"] for n in response.json())
    assert keys == ["a", "c"]


async def test_api_notes_search_matches_against_tags(
    client: httpx.AsyncClient,
) -> None:
    await notes.upsert(app.state.db, user_id=1, key="x", content="something", tags="urgent")
    await notes.upsert(app.state.db, user_id=1, key="y", content="something else", tags="later")

    response = await client.get("/api/notes?q=urgent", headers=AUTH)
    assert response.status_code == 200
    assert [n["key"] for n in response.json()] == ["x"]


async def test_api_notes_search_empty_query_returns_full_list(
    client: httpx.AsyncClient,
) -> None:
    await notes.upsert(app.state.db, user_id=1, key="a", content="alpha")
    await notes.upsert(app.state.db, user_id=1, key="b", content="beta")

    response = await client.get("/api/notes?q=", headers=AUTH)
    assert response.status_code == 200
    keys = sorted(n["key"] for n in response.json())
    assert keys == ["a", "b"]


async def test_api_notes_search_tolerates_fts_special_chars(
    client: httpx.AsyncClient,
) -> None:
    # FTS5 raw syntax rejects bare punctuation like quotes/colons and treats
    # bare operators like AND/OR/NOT as syntax. The route has to sanitise
    # the query before MATCH, otherwise the user gets a 500 the moment they
    # type something like `it's:` or `AND` into the search box.
    await notes.upsert(app.state.db, user_id=1, key="k", content="alpha bravo")

    # Quotes around a known term still hit.
    response = await client.get('/api/notes?q="alpha"', headers=AUTH)
    assert response.status_code == 200
    assert [n["key"] for n in response.json()] == ["k"]

    # A bare FTS5 operator must not 500 (this is what would happen without
    # the sanitiser; raw `MATCH 'AND'` raises OperationalError).
    response = await client.get("/api/notes?q=AND", headers=AUTH)
    assert response.status_code == 200

    # Operator characters get stripped, so `:` becomes "foobar" — proves
    # the colon isn't passed through to FTS5.
    response = await client.get("/api/notes?q=foo:bar", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == []


async def test_api_notes_search_whitespace_only_returns_full_list(
    client: httpx.AsyncClient,
) -> None:
    # `?q=` (empty) and `?q=%20%20%20` (whitespace) should be symmetric —
    # both fall through to list_all rather than the empty-result branch.
    await notes.upsert(app.state.db, user_id=1, key="a", content="alpha")
    await notes.upsert(app.state.db, user_id=1, key="b", content="beta")

    response = await client.get("/api/notes?q=%20%20%20", headers=AUTH)
    assert response.status_code == 200
    keys = sorted(n["key"] for n in response.json())
    assert keys == ["a", "b"]
