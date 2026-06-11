from fastapi.testclient import TestClient

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def test_auth_me_returns_admin_identity() -> None:
    with TestClient(app) as client:
        r = client.get("/api/auth/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == 1
    assert body["role"] == "admin"
    assert "bootstrap_completed" in body


def test_auth_me_requires_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401


def test_logout_invalidates_session() -> None:
    with TestClient(app) as client:
        assert client.post("/api/auth/logout", headers=AUTH).status_code == 200
        # session row deleted → the same token no longer resolves
        assert client.get("/api/auth/me", headers=AUTH).status_code == 401
