import pytest
from fastapi.testclient import TestClient

from hermes.main import app


def test_lifespan_initializes_db_and_serves_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_SIGNAL_NUMBER", raising=False)
    with TestClient(app) as client:
        assert app.state.db is not None
        assert app.state.signal_worker is None
        assert client.get("/healthz").status_code == 200
