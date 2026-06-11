import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient
from sqlalchemy import text

from hermes.identity import hash_token
from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture
async def client(pg_db):
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


def test_auth_me_returns_admin_identity(pg_db) -> None:
    with TestClient(app) as client:
        r = client.get("/api/auth/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == 1
    assert body["role"] == "platform_admin"
    assert "bootstrap_completed" in body


def test_auth_me_requires_auth(pg_db) -> None:
    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401


async def test_logout_keeps_bootstrap_admin_session(client: httpx.AsyncClient) -> None:
    # The env bootstrap token is infra, not API-revocable: logout must NOT
    # delete its session, or the operator is locked out until restart.
    assert (await client.post("/api/auth/logout", headers=AUTH)).status_code == 200
    # The same token still resolves → admin is not bricked.
    assert (await client.get("/api/auth/me", headers=AUTH)).status_code == 200


async def test_logout_deletes_a_real_session(client: httpx.AsyncClient) -> None:
    # A non-bootstrap (e.g. C2 magic-link) session is revocable normally.
    real_token = "real-user-token"
    real_auth = {"Authorization": f"Bearer {real_token}"}
    # `sessions` is RLS-locked and the holzi_app engine has app.user_id unset,
    # so seed the fixture row via the owner engine (bypasses RLS) — mirrors how
    # the lifespan seeds the bootstrap session.
    async with app.state.owner_db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sessions(user_id, token_hash, label, created_at, expires_at) "
                "VALUES (1, :h, 'web', 0, NULL)"
            ),
            {"h": hash_token(real_token)},
        )
    # The real session resolves before logout.
    assert (await client.get("/api/auth/me", headers=real_auth)).status_code == 200
    assert (await client.post("/api/auth/logout", headers=real_auth)).status_code == 200
    # Real session is gone…
    assert (await client.get("/api/auth/me", headers=real_auth)).status_code == 401
    # …but the bootstrap admin is untouched.
    assert (await client.get("/api/auth/me", headers=AUTH)).status_code == 200
