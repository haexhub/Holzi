"""HTTP API tests for /api/skills and /api/personas/{id}/skills (Plan 33)."""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app

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


def _skill_body(**overrides):
    body = {
        "slug": "code-style-typescript",
        "name": "Code Style TypeScript",
        "description": "TS code review guidelines",
        "when_to_use": "Bei TypeScript-Code-Reviews",
        "body_markdown": "Achte auf strict-null-checks.",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_skills_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/skills")
    assert response.status_code == 401


async def test_persona_skills_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/personas/1/skills")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# /api/skills CRUD
# ---------------------------------------------------------------------------


async def test_list_skills_empty_on_fresh_db(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/skills", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"skills": []}


async def test_create_skill_returns_201(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/skills", headers=AUTH, json=_skill_body()
    )
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "code-style-typescript"
    assert body["name"] == "Code Style TypeScript"
    assert body["description"] == "TS code review guidelines"
    assert body["when_to_use"] == "Bei TypeScript-Code-Reviews"
    assert body["body_markdown"] == "Achte auf strict-null-checks."
    assert isinstance(body["id"], int)
    assert isinstance(body["created_at"], int)
    assert body["created_at"] == body["updated_at"]


async def test_create_skill_without_when_to_use_is_accepted(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/skills",
        headers=AUTH,
        json=_skill_body(when_to_use=None, slug="brief"),
    )
    assert response.status_code == 201
    assert response.json()["when_to_use"] is None


async def test_create_duplicate_slug_returns_409(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/skills", headers=AUTH, json=_skill_body())
    response = await client.post(
        "/api/skills",
        headers=AUTH,
        json=_skill_body(name="Different name"),
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "SKILL_SLUG_CONFLICT"
    assert detail["params"]["slug"] == "code-style-typescript"


async def test_create_skill_validates_slug_format(
    client: httpx.AsyncClient,
) -> None:
    for bad_slug in ("Has-Caps", "-leading-dash", "trailing-dash-", "white space"):
        response = await client.post(
            "/api/skills", headers=AUTH, json=_skill_body(slug=bad_slug)
        )
        assert response.status_code == 422, bad_slug
        assert response.json()["detail"] == "SKILL_INVALID_SLUG", bad_slug


async def test_create_skill_accepts_single_char_slug(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/skills", headers=AUTH, json=_skill_body(slug="x")
    )
    assert response.status_code == 201


async def test_create_skill_rejects_oversized_body(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/skills",
        headers=AUTH,
        json=_skill_body(body_markdown="x" * (16 * 1024 + 1)),
    )
    assert response.status_code == 422


async def test_create_skill_rejects_empty_required_fields(
    client: httpx.AsyncClient,
) -> None:
    for field in ("name", "description", "body_markdown"):
        response = await client.post(
            "/api/skills", headers=AUTH, json=_skill_body(**{field: ""})
        )
        assert response.status_code == 422, field


async def test_update_skill_partial_fields(
    client: httpx.AsyncClient,
) -> None:
    create = await client.post(
        "/api/skills", headers=AUTH, json=_skill_body()
    )
    skill_id = create.json()["id"]
    response = await client.put(
        f"/api/skills/{skill_id}",
        headers=AUTH,
        json={"body_markdown": "Updated body"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["body_markdown"] == "Updated body"
    assert body["slug"] == "code-style-typescript"


async def test_update_unknown_skill_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/skills/99999",
        headers=AUTH,
        json={"body_markdown": "x"},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "SKILL_NOT_FOUND"
    assert detail["params"]["id"] == 99999


async def test_delete_skill_returns_204(client: httpx.AsyncClient) -> None:
    create = await client.post(
        "/api/skills", headers=AUTH, json=_skill_body()
    )
    skill_id = create.json()["id"]
    response = await client.delete(f"/api/skills/{skill_id}", headers=AUTH)
    assert response.status_code == 204


async def test_delete_unknown_skill_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/api/skills/99999", headers=AUTH)
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "SKILL_NOT_FOUND"
    assert detail["params"]["id"] == 99999


async def test_list_skills_returns_inserted(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/skills",
        headers=AUTH,
        json=_skill_body(slug="a", name="Alpha"),
    )
    await client.post(
        "/api/skills",
        headers=AUTH,
        json=_skill_body(slug="b", name="Beta"),
    )
    response = await client.get("/api/skills", headers=AUTH)
    assert response.status_code == 200
    skills = response.json()["skills"]
    assert [s["slug"] for s in skills] == ["a", "b"]


# ---------------------------------------------------------------------------
# /api/personas/{id}/skills
# ---------------------------------------------------------------------------


async def _default_persona_id(client: httpx.AsyncClient) -> int:
    response = await client.get("/api/personas", headers=AUTH)
    return response.json()["personas"][0]["id"]


async def test_get_persona_skills_empty(client: httpx.AsyncClient) -> None:
    persona_id = await _default_persona_id(client)
    response = await client.get(
        f"/api/personas/{persona_id}/skills", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"skills": []}


async def test_get_persona_skills_unknown_persona_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/personas/99999/skills", headers=AUTH)
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_NOT_FOUND"
    assert detail["params"]["id"] == 99999


async def test_set_persona_skills_inserts_in_order(
    client: httpx.AsyncClient,
) -> None:
    persona_id = await _default_persona_id(client)
    a = (
        await client.post(
            "/api/skills",
            headers=AUTH,
            json=_skill_body(slug="a", name="A"),
        )
    ).json()
    b = (
        await client.post(
            "/api/skills",
            headers=AUTH,
            json=_skill_body(slug="b", name="B"),
        )
    ).json()

    response = await client.put(
        f"/api/personas/{persona_id}/skills",
        headers=AUTH,
        json={
            "items": [
                {"skill_id": b["id"], "ordering": 0, "enabled": True},
                {"skill_id": a["id"], "ordering": 1, "enabled": False},
            ]
        },
    )
    assert response.status_code == 200
    items = response.json()["skills"]
    assert len(items) == 2
    assert items[0]["skill"]["slug"] == "b"
    assert items[0]["ordering"] == 0
    assert items[0]["enabled"] is True
    assert items[1]["skill"]["slug"] == "a"
    assert items[1]["ordering"] == 1
    assert items[1]["enabled"] is False


async def test_set_persona_skills_empty_clears_list(
    client: httpx.AsyncClient,
) -> None:
    persona_id = await _default_persona_id(client)
    a = (
        await client.post(
            "/api/skills", headers=AUTH, json=_skill_body(slug="a")
        )
    ).json()
    await client.put(
        f"/api/personas/{persona_id}/skills",
        headers=AUTH,
        json={
            "items": [
                {"skill_id": a["id"], "ordering": 0, "enabled": True}
            ]
        },
    )
    response = await client.put(
        f"/api/personas/{persona_id}/skills",
        headers=AUTH,
        json={"items": []},
    )
    assert response.status_code == 200
    assert response.json() == {"skills": []}


async def test_set_persona_skills_with_unknown_skill_returns_422(
    client: httpx.AsyncClient,
) -> None:
    persona_id = await _default_persona_id(client)
    response = await client.put(
        f"/api/personas/{persona_id}/skills",
        headers=AUTH,
        json={
            "items": [
                {"skill_id": 99999, "ordering": 0, "enabled": True}
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "PERSONA_SKILL_ACTIVATION_INVALID"


async def test_set_persona_skills_unknown_persona_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/api/personas/99999/skills",
        headers=AUTH,
        json={"items": []},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "PERSONA_NOT_FOUND"
    assert detail["params"]["id"] == 99999


async def test_delete_skill_cascades_persona_link(
    client: httpx.AsyncClient,
) -> None:
    persona_id = await _default_persona_id(client)
    a = (
        await client.post(
            "/api/skills", headers=AUTH, json=_skill_body(slug="a")
        )
    ).json()
    await client.put(
        f"/api/personas/{persona_id}/skills",
        headers=AUTH,
        json={
            "items": [
                {"skill_id": a["id"], "ordering": 0, "enabled": True}
            ]
        },
    )

    await client.delete(f"/api/skills/{a['id']}", headers=AUTH)

    response = await client.get(
        f"/api/personas/{persona_id}/skills", headers=AUTH
    )
    assert response.json() == {"skills": []}
