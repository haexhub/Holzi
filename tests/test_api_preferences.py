"""HTTP API tests for /api/personas + history + /api/channels.

Plan 36 reshaped the persona wire contract from a single ``prompt``
column to three fragments (``soul``/``identity``/``agents``) and added
the history list + restore endpoints. After Task 5, ``ensure_backfill``
seeds the default persona with the fragments shape natively — no
monkeypatching of the lifespan boot is needed.
"""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes import main as hermes_main
from hermes.personas import (
    CHANNEL_REGISTRY,
    DEFAULT_PERSONA_AGENTS,
    DEFAULT_PERSONA_IDENTITY,
    DEFAULT_PERSONA_NAME,
    DEFAULT_PERSONA_SOUL,
)

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
async def client(pg_db):
    async with (
        LifespanManager(app=hermes_main.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hermes_main.app),
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
# Personas — list / create / update / delete (fragments shape)
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
    assert p["soul"] == DEFAULT_PERSONA_SOUL
    assert p["identity"] == DEFAULT_PERSONA_IDENTITY
    assert p["agents"] == DEFAULT_PERSONA_AGENTS
    assert isinstance(p["created_at"], int)
    assert isinstance(p["updated_at"], int)
    assert "prompt" not in p


async def test_create_persona_returns_201_with_fragments(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={
            "name": "Reviewer",
            "soul": "Be picky.",
            "identity": "You are the review bot.",
            "agents": "Open one thread per finding.",
            "is_default": False,
        },
    )
    assert response.status_code == 201
    p = response.json()
    assert p["name"] == "Reviewer"
    assert p["soul"] == "Be picky."
    assert p["identity"] == "You are the review bot."
    assert p["agents"] == "Open one thread per finding."
    assert p["is_default"] is False


async def test_create_persona_with_only_identity_succeeds(
    client: httpx.AsyncClient,
) -> None:
    """Single non-empty fragment is enough — only the all-empty case is
    rejected."""
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Minimal", "identity": "just-id"},
    )
    assert response.status_code == 201
    p = response.json()
    assert p["soul"] == ""
    assert p["identity"] == "just-id"
    assert p["agents"] == ""


async def test_create_persona_with_prompt_key_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """Legacy ``prompt`` field must be rejected by ``extra="forbid"`` —
    no transition shim. Pydantic's structured 422 isn't our
    ``{code, params}`` shape; we only assert status + that the offending
    field name is surfaced for debuggability."""
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Legacy", "prompt": "old-school", "is_default": False},
    )
    assert response.status_code == 422
    # Pydantic's default error structure carries the field name somewhere
    # in the JSON; we just check it's present so the FE/operator can see
    # what got rejected.
    assert "prompt" in response.text


async def test_create_persona_all_fragments_empty_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Empty", "soul": "", "identity": "", "agents": ""},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_FRAGMENTS_ALL_EMPTY"
    assert detail["params"] == {}


async def test_create_persona_only_whitespace_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """Whitespace-only fragments count as empty after `.strip()`."""
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={
            "name": "Blanks",
            "soul": "   ",
            "identity": "\n\t",
            "agents": "",
        },
    )
    assert response.status_code == 422
    assert (
        response.json()["detail"]["code"] == "PERSONA_FRAGMENTS_ALL_EMPTY"
    )


async def test_create_persona_duplicate_name_returns_409(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Twin", "identity": "first"},
    )
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Twin", "identity": "second"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_NAME_CONFLICT"
    assert detail["params"]["name"] == "Twin"


async def test_create_persona_fragment_too_long_returns_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "X", "identity": "a" * 8193},
    )
    assert response.status_code == 422


async def test_update_persona_replaces_single_fragment(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Edit-Me", "identity": "v1"},
    )
    pid = created.json()["id"]

    response = await client.put(
        f"/api/personas/{pid}",
        headers=AUTH,
        json={"identity": "v2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["identity"] == "v2"
    assert body["soul"] == ""
    assert body["agents"] == ""


async def test_update_persona_clearing_only_fragment_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """A persona with only ``identity`` set; PUT that empties identity →
    the post-merge state would have all three fragments empty. Refuse."""
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Solo", "identity": "x"},
    )
    pid = created.json()["id"]

    response = await client.put(
        f"/api/personas/{pid}",
        headers=AUTH,
        json={"identity": ""},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_FRAGMENTS_ALL_EMPTY"
    assert detail["params"] == {}


async def test_update_persona_clearing_fragment_with_other_set_succeeds(
    client: httpx.AsyncClient,
) -> None:
    """Clearing one fragment is fine as long as another is still
    non-empty after merge."""
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Multi", "soul": "S", "identity": "I"},
    )
    pid = created.json()["id"]

    response = await client.put(
        f"/api/personas/{pid}",
        headers=AUTH,
        json={"soul": ""},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["soul"] == ""
    assert body["identity"] == "I"


async def test_update_persona_with_prompt_key_returns_422(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Strict", "identity": "x"},
    )
    pid = created.json()["id"]
    response = await client.put(
        f"/api/personas/{pid}",
        headers=AUTH,
        json={"prompt": "still-legacy"},
    )
    assert response.status_code == 422
    assert "prompt" in response.text


async def test_update_persona_is_default_demotes_others(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "New", "identity": "p"},
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
        json={"identity": "x"},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_NOT_FOUND"
    assert detail["params"]["id"] == 99999


async def test_delete_default_persona_returns_422(
    client: httpx.AsyncClient,
) -> None:
    listing = await client.get("/api/personas", headers=AUTH)
    default_id = listing.json()["personas"][0]["id"]

    response = await client.delete(
        f"/api/personas/{default_id}", headers=AUTH
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "PERSONA_DEFAULT_DELETE"


async def test_delete_non_default_clears_channel_assignment(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Tmp", "identity": "p"},
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
# Persona history — list / restore
# ---------------------------------------------------------------------------


async def test_history_unknown_persona_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/personas/9999/history", headers=AUTH)
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_NOT_FOUND"
    assert detail["params"]["id"] == 9999


async def test_history_lists_create_then_update(
    client: httpx.AsyncClient,
) -> None:
    """After a create + an update, history has 2 rows newest first, each
    with a parsed ``snapshot`` dict carrying the four fragment fields."""
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Trace", "identity": "v1"},
    )
    pid = created.json()["id"]
    await client.put(
        f"/api/personas/{pid}",
        headers=AUTH,
        json={"identity": "v2"},
    )

    response = await client.get(
        f"/api/personas/{pid}/history", headers=AUTH
    )
    assert response.status_code == 200
    history = response.json()["history"]
    assert len(history) == 2
    # Newest first — the update row precedes the create row.
    assert history[0]["snapshot"]["identity"] == "v2"
    assert history[1]["snapshot"]["identity"] == "v1"
    for row in history:
        assert row["persona_id"] == pid
        snap = row["snapshot"]
        assert set(snap.keys()) == {"name", "soul", "identity", "agents"}
        assert snap["name"] == "Trace"
        assert isinstance(row["created_at"], int)


async def test_restore_happy_path(client: httpx.AsyncClient) -> None:
    """state-1 → state-2 → restore-to-state-1 → live persona matches
    state-1 and history grows to 3 rows."""
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "Hop", "soul": "S1", "identity": "I1"},
    )
    pid = created.json()["id"]

    # Grab the create-snapshot id (newest in 1-row history).
    history = (
        await client.get(f"/api/personas/{pid}/history", headers=AUTH)
    ).json()["history"]
    assert len(history) == 1
    snapshot_v1_id = history[0]["id"]

    # Move to state-2.
    await client.put(
        f"/api/personas/{pid}",
        headers=AUTH,
        json={"soul": "S2", "identity": "I2"},
    )

    restore = await client.post(
        f"/api/personas/{pid}/history/{snapshot_v1_id}/restore",
        headers=AUTH,
    )
    assert restore.status_code == 200
    restored = restore.json()
    assert restored["soul"] == "S1"
    assert restored["identity"] == "I1"
    assert restored["name"] == "Hop"

    # Three rows now: create (v1), update (v2), restore (v1 again).
    history_after = (
        await client.get(f"/api/personas/{pid}/history", headers=AUTH)
    ).json()["history"]
    assert len(history_after) == 3
    assert history_after[0]["snapshot"]["identity"] == "I1"
    assert history_after[1]["snapshot"]["identity"] == "I2"
    assert history_after[2]["snapshot"]["identity"] == "I1"


async def test_restore_unknown_snapshot_returns_404(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "WithHistory", "identity": "x"},
    )
    pid = created.json()["id"]

    response = await client.post(
        f"/api/personas/{pid}/history/99999/restore", headers=AUTH
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_HISTORY_NOT_FOUND"
    assert detail["params"]["id"] == 99999


async def test_restore_unknown_persona_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/personas/9999/history/1/restore", headers=AUTH
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_NOT_FOUND"
    assert detail["params"]["id"] == 9999


async def test_restore_mismatched_persona_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """Snapshot belongs to persona A; restore endpoint hit on persona B's
    URL → 422 PERSONA_HISTORY_PERSONA_MISMATCH."""
    a = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "A", "identity": "from-A"},
    )
    a_id = a.json()["id"]
    b = await client.post(
        "/api/personas",
        headers=AUTH,
        json={"name": "B", "identity": "from-B"},
    )
    b_id = b.json()["id"]

    a_history = (
        await client.get(f"/api/personas/{a_id}/history", headers=AUTH)
    ).json()["history"]
    a_snapshot = a_history[0]["id"]

    response = await client.post(
        f"/api/personas/{b_id}/history/{a_snapshot}/restore", headers=AUTH
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_HISTORY_PERSONA_MISMATCH"
    assert detail["params"]["persona_id"] == b_id
    assert detail["params"]["snapshot_id"] == a_snapshot


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


# ---------------------------------------------------------------------------
# Persona credential + model (Plan 29-D Task 4)
# ---------------------------------------------------------------------------


async def test_put_persona_unknown_credential_422(client: httpx.AsyncClient) -> None:
    resp = await client.put(
        "/api/personas/1",
        json={"llm_credential_id": 9999},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "PERSONA_INVALID_CREDENTIAL"


async def test_put_persona_model_without_credential_422(client: httpx.AsyncClient) -> None:
    resp = await client.put(
        "/api/personas/1",
        json={"model": "gpt-4o"},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "PERSONA_INVALID_MODEL"


async def test_put_persona_credential_persisted(client: httpx.AsyncClient) -> None:
    # Create a credential to pin
    create_resp = await client.post(
        "/api/llm/credentials",
        json={"provider": "openai", "display_name": "test-openai", "api_key": "sk-test"},
        headers=AUTH,
    )
    assert create_resp.status_code == 201
    cred_id = create_resp.json()["id"]

    # Pin it on persona 1 (without model, which would require provider model validation)
    put_resp = await client.put(
        "/api/personas/1",
        json={"llm_credential_id": cred_id},
        headers=AUTH,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["llm_credential_id"] == cred_id
    assert put_resp.json()["model"] is None


async def test_put_persona_clear_credential(client: httpx.AsyncClient) -> None:
    # First pin a credential
    create_resp = await client.post(
        "/api/llm/credentials",
        json={"provider": "openai", "display_name": "test-openai2", "api_key": "sk-test"},
        headers=AUTH,
    )
    cred_id = create_resp.json()["id"]
    await client.put("/api/personas/1", json={"llm_credential_id": cred_id}, headers=AUTH)

    # Now clear it
    clear_resp = await client.put(
        "/api/personas/1",
        json={"llm_credential_id": None},
        headers=AUTH,
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["llm_credential_id"] is None


async def test_get_persona_models_no_credential_503(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/personas/1/models", headers=AUTH)
    assert resp.status_code == 503


async def test_get_persona_models_persona_not_found_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/personas/9999/models", headers=AUTH)
    assert resp.status_code == 404


async def test_delete_credential_nulls_persona_model(client: httpx.AsyncClient) -> None:
    """Deleting a credential nulls llm_credential_id (via FK cascade) and model (app-side)."""
    # Create a credential
    cred_resp = await client.post(
        "/api/llm/credentials",
        json={"provider": "openai", "display_name": "to-delete", "api_key": "sk-x"},
        headers=AUTH,
    )
    assert cred_resp.status_code == 201
    cred_id = cred_resp.json()["id"]

    # Pin it on persona 1 (no model, just credential)
    put_resp = await client.put(
        "/api/personas/1",
        json={"llm_credential_id": cred_id},
        headers=AUTH,
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["llm_credential_id"] == cred_id

    # Delete the credential
    del_resp = await client.delete(f"/api/llm/credentials/{cred_id}", headers=AUTH)
    assert del_resp.status_code == 204

    # Verify persona no longer references the credential (both columns nulled)
    list_resp = await client.get("/api/personas", headers=AUTH)
    p = list_resp.json()["personas"][0]
    assert p["llm_credential_id"] is None
    assert p["model"] is None
