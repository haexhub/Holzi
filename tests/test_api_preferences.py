"""HTTP API tests for /api/personas and /api/channels (Plan 29-A)."""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.personas import CHANNEL_REGISTRY, DEFAULT_PERSONA_NAME

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
# Auth
# ---------------------------------------------------------------------------


async def test_personas_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/personas")
    assert response.status_code == 401


async def test_channels_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/channels")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------


async def test_list_personas_returns_backfilled_default(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/personas", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert len(body["personas"]) == 1
    p = body["personas"][0]
    assert p["name"] == DEFAULT_PERSONA_NAME
    assert p["is_default"] is True
    assert p["prompt"]
    assert isinstance(p["created_at"], int)
    assert isinstance(p["updated_at"], int)


async def test_create_persona_returns_201(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Reviewer", "prompt": "Be picky.", "is_default": False},
    )
    assert response.status_code == 201
    p = response.json()
    assert p["name"] == "Reviewer"
    assert p["prompt"] == "Be picky."
    assert p["is_default"] is False


async def test_create_persona_duplicate_name_returns_409(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Twin", "prompt": "first"},
    )
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Twin", "prompt": "second"},
    )
    assert response.status_code == 409


async def test_create_persona_blank_prompt_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/personas", headers=AUTH, json={"name": "X", "prompt": ""}
    )
    assert response.status_code == 422


async def test_create_persona_prompt_too_long_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "X", "prompt": "a" * 8193},
    )
    assert response.status_code == 422


async def test_update_persona_is_default_demotes_others(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "New", "prompt": "p"},
    )
    new_id = created.json()["id"]

    response = await client.put(
        f"/api/personas/{new_id}",
        headers=AUTH,
        json={"is_default": True},
    )
    assert response.status_code == 200
    assert response.json()["is_default"] is True

    listing = await client.get("/api/personas", headers=AUTH)
    defaults = [
        p["name"] for p in listing.json()["personas"] if p["is_default"]
    ]
    assert defaults == ["New"]


async def test_update_persona_missing_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/personas/99999",
        headers=AUTH,
        json={"prompt": "x"},
    )
    assert response.status_code == 404


async def test_delete_default_persona_returns_422(
    client: httpx.AsyncClient,
) -> None:
    listing = await client.get("/api/personas", headers=AUTH)
    default_id = listing.json()["personas"][0]["id"]

    response = await client.delete(
        f"/api/personas/{default_id}", headers=AUTH
    )
    assert response.status_code == 422


async def test_delete_non_default_clears_channel_assignment(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Tmp", "prompt": "p"},
    )
    tmp_id = created.json()["id"]

    assign = await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={"default_persona_id": tmp_id},
    )
    assert assign.status_code == 200
    assert assign.json()["default_persona_id"] == tmp_id

    delete = await client.delete(f"/api/personas/{tmp_id}", headers=AUTH)
    assert delete.status_code == 204

    after = await client.get("/api/channels", headers=AUTH)
    web = next(
        c for c in after.json()["channels"] if c["channel"] == "web"
    )
    assert web["default_persona_id"] is None


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


async def test_list_channels_returns_registry_seeded_rows(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/channels", headers=AUTH)
    assert response.status_code == 200
    rows = response.json()["channels"]
    assert [r["channel"] for r in rows] == list(CHANNEL_REGISTRY.keys())
    for row in rows:
        registry = CHANNEL_REGISTRY[row["channel"]]
        assert row["label"] == registry["label"]
        assert row["default_prompt"] == registry["default_prompt"]
        assert row["prompt"] == registry["default_prompt"]
        assert row["is_default_prompt"] is True
        assert row["default_persona_id"] is None
        assert isinstance(row["updated_at"], int)


async def test_update_channel_prompt_marks_non_default(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={"prompt": "Custom web prompt."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == "Custom web prompt."
    assert body["is_default_prompt"] is False


async def test_update_channel_unknown_channel_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/channels/discord",
        headers=AUTH,
        json={"prompt": "x"},
    )
    assert response.status_code == 404


async def test_update_channel_unknown_persona_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={"default_persona_id": 99999},
    )
    assert response.status_code == 422


async def test_update_channel_persona_null_clears_assignment(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Pick", "prompt": "p"},
    )
    pid = created.json()["id"]
    await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={"default_persona_id": pid},
    )

    response = await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={"default_persona_id": None},
    )
    assert response.status_code == 200
    assert response.json()["default_persona_id"] is None


async def test_reset_channel_prompt_restores_default(
    client: httpx.AsyncClient,
) -> None:
    await client.put(
        "/api/channels/signal",
        headers=AUTH,
        json={"prompt": "Custom signal prompt."},
    )

    response = await client.post(
        "/api/channels/signal/reset", headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == CHANNEL_REGISTRY["signal"]["default_prompt"]
    assert body["is_default_prompt"] is True


async def test_reset_unknown_channel_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/channels/discord/reset", headers=AUTH
    )
    assert response.status_code == 404
