import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import reminders

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


async def test_api_reminders_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/reminders")
    assert response.status_code == 401


async def test_api_reminders_list_excludes_fired_by_default(
    client: httpx.AsyncClient,
) -> None:
    r1 = await reminders.create(
        app.state.db, due_at=2_000_000_000, message="future"
    )
    r2 = await reminders.create(
        app.state.db, due_at=1_700_000_000, message="past"
    )
    await reminders.mark_fired(app.state.db, r2.id)

    response = await client.get("/api/reminders", headers=AUTH)
    assert response.status_code == 200
    ids = [r["id"] for r in response.json()]
    assert ids == [r1.id]


async def test_api_reminders_list_include_fired_returns_all(
    client: httpx.AsyncClient,
) -> None:
    r1 = await reminders.create(
        app.state.db, due_at=2_000_000_000, message="future"
    )
    r2 = await reminders.create(
        app.state.db, due_at=1_700_000_000, message="past"
    )
    await reminders.mark_fired(app.state.db, r2.id)

    response = await client.get(
        "/api/reminders?include_fired=true", headers=AUTH
    )
    assert response.status_code == 200
    ids = {r["id"] for r in response.json()}
    assert ids == {r1.id, r2.id}


async def test_api_reminders_post_creates(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/reminders",
        headers=AUTH,
        json={"due_at": 2_000_000_000, "message": "wake up"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["due_at"] == 2_000_000_000
    assert data["message"] == "wake up"
    assert data["channel"] == "signal"
    assert data["fired_at"] is None


async def test_api_reminders_delete_removes(client: httpx.AsyncClient) -> None:
    r = await reminders.create(
        app.state.db, due_at=2_000_000_000, message="cancel me"
    )
    response = await client.delete(f"/api/reminders/{r.id}", headers=AUTH)
    assert response.status_code == 204

    remaining = await reminders.list_all(app.state.db, include_fired=True)
    assert all(rem.id != r.id for rem in remaining)


async def test_api_reminders_delete_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/reminders/99999", headers=AUTH)
    assert response.status_code == 404
