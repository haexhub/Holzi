import pytest
from fastapi.testclient import TestClient

from hermes import main as hermes_main
from hermes.main import app


def test_lifespan_initializes_db_and_serves_health(pg_db) -> None:
    with TestClient(app) as client:
        assert app.state.db is not None
        assert client.get("/healthz").status_code == 200


def test_lifespan_cleans_up_when_init_db_fails(
    pg_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom() -> None:
        raise RuntimeError("simulated db init failure")

    monkeypatch.setattr(hermes_main, "init_db", boom)

    with (
        pytest.raises(RuntimeError, match="simulated db init failure"),
        TestClient(app),
    ):
        pass
