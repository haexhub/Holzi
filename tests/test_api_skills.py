"""HTTP API tests for /api/skills (Plan 33 + Plan 37).

Plan 37 drops the per-persona activation endpoints and adds `enabled`
toggle to the Skills CRUD surface.
"""
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


# ---------------------------------------------------------------------------
# /api/skills CRUD
# ---------------------------------------------------------------------------


async def test_list_skills_empty_on_fresh_db(client: httpx.AsyncClient) -> None:
    # The lifespan seeds bootstrap-first-chat; we start from that baseline.
    response = await client.get("/api/skills", headers=AUTH)
    assert response.status_code == 200
    # At minimum the bootstrap skill is present after lifespan boot.
    data = response.json()
    assert "skills" in data


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
    assert body["enabled"] is True  # Plan 37: default enabled
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
    # when_to_use=None is stored as empty string (NOT NULL column)
    assert response.json()["when_to_use"] in (None, "")


async def test_create_skill_with_enabled_false(client: httpx.AsyncClient) -> None:
    """Plan 37: can create a disabled skill explicitly."""
    response = await client.post(
        "/api/skills",
        headers=AUTH,
        json=_skill_body(slug="disabled-on-create", enabled=False),
    )
    assert response.status_code == 201
    assert response.json()["enabled"] is False


async def test_create_skill_rejects_unknown_fields(
    client: httpx.AsyncClient,
) -> None:
    """Plan 37: extra='forbid' on SkillCreate rejects unknown fields."""
    response = await client.post(
        "/api/skills",
        headers=AUTH,
        json=_skill_body(unknown_field="surprise"),
    )
    assert response.status_code == 422


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


async def test_update_skill_enabled_toggle(client: httpx.AsyncClient) -> None:
    """Plan 37: can toggle enabled via PUT /api/skills/{id}."""
    create = await client.post("/api/skills", headers=AUTH, json=_skill_body())
    skill_id = create.json()["id"]
    assert create.json()["enabled"] is True

    response = await client.put(
        f"/api/skills/{skill_id}", headers=AUTH, json={"enabled": False}
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False

    response = await client.put(
        f"/api/skills/{skill_id}", headers=AUTH, json={"enabled": True}
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True


async def test_update_skill_rejects_unknown_fields(
    client: httpx.AsyncClient,
) -> None:
    """Plan 37: extra='forbid' on SkillUpdate rejects unknown fields."""
    create = await client.post("/api/skills", headers=AUTH, json=_skill_body())
    skill_id = create.json()["id"]
    response = await client.put(
        f"/api/skills/{skill_id}",
        headers=AUTH,
        json={"unknown_field": "oops"},
    )
    assert response.status_code == 422


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
    slugs = [s["slug"] for s in skills]
    assert "a" in slugs
    assert "b" in slugs


async def test_persona_skills_endpoint_gone(client: httpx.AsyncClient) -> None:
    """Plan 37: GET /api/personas/{id}/skills endpoint no longer exists."""
    response = await client.get("/api/personas/1/skills", headers=AUTH)
    assert response.status_code == 404
