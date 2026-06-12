"""HTTP API tests for /api/channels.

Plan 36 reshaped the persona wire contract from a single ``prompt``
column to three fragments (``soul``/``identity``/``agents``) and added
the history list + restore endpoints. After Task 5, ``ensure_backfill``
seeds the default persona with the fragments shape natively — no
monkeypatching of the lifespan boot is needed.
"""
import httpx

from hermes.personas import (
    CHANNEL_REGISTRY,
)

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}




# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_channels_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/channels")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Channels — untouched by Plan 36, but kept for regression coverage.
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
    detail = response.json()["detail"]
    assert detail["code"] == "CHANNEL_NOT_FOUND"
    assert detail["params"]["channel"] == "discord"


async def test_update_channel_rejects_explicit_null_prompt(
    client: httpx.AsyncClient,
) -> None:
    # Omitting `prompt` leaves it unchanged, but an explicit null has no valid
    # meaning (reset has its own endpoint) and is rejected at validation.
    response = await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={"prompt": None},
    )
    assert response.status_code == 422


async def test_update_channel_unknown_persona_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/channels/web",
        headers=AUTH,
        json={"default_persona_id": 99999},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_REF_INVALID"
    assert detail["params"]["id"] == 99999


async def test_update_channel_persona_null_clears_assignment(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Pick", "identity": "p"},
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
        "/api/channels/task",
        headers=AUTH,
        json={"prompt": "Custom task prompt."},
    )

    response = await client.post(
        "/api/channels/task/reset", headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == CHANNEL_REGISTRY["task"]["default_prompt"]
    assert body["is_default_prompt"] is True


async def test_reset_unknown_channel_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/channels/discord/reset", headers=AUTH
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "CHANNEL_NOT_FOUND"
    assert detail["params"]["channel"] == "discord"
