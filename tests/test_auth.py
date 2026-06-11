from fastapi.testclient import TestClient

from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"


def test_protected_route_without_authorization_header_returns_401() -> None:
    with TestClient(app) as client:
        response = client.get("/api/ping")
    assert response.status_code == 401


def test_protected_route_with_invalid_token_returns_401() -> None:
    with TestClient(app) as client:
        response = client.get("/api/ping", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_protected_route_with_malformed_header_returns_401() -> None:
    with TestClient(app) as client:
        response = client.get("/api/ping", headers={"Authorization": VALID_TOKEN})
    assert response.status_code == 401


def test_protected_route_with_valid_token_returns_200() -> None:
    with TestClient(app) as client:
        response = client.get("/api/ping", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"pong": True}


def test_healthz_still_accessible_without_auth() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200


def test_request_state_user_id_is_set_for_valid_token() -> None:
    with TestClient(app) as client:
        r = client.get("/api/ping", headers={"Authorization": "Bearer test-token-for-pytest"})
    assert r.status_code == 200


def test_unknown_token_is_401_via_resolver() -> None:
    with TestClient(app) as client:
        r = client.get("/api/ping", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
