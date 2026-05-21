from fastapi.testclient import TestClient

from hermes.main import app


def test_lifespan_initializes_db_and_serves_health() -> None:
    with TestClient(app) as client:
        assert app.state.db is not None
        assert client.get("/healthz").status_code == 200
