import os
import tempfile

# Tests need a file-based SQLite path (not :memory:) so the default
# AsyncAdaptedQueuePool can hand out one connection per concurrent task.
# With :memory: + StaticPool, the reminder scheduler's background loop
# would share a single connection with whatever the test is doing and
# race on transaction state.
_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db", prefix="hermes-test-")
os.close(_TEST_DB_FD)
os.unlink(_TEST_DB_PATH)  # init_db will recreate; we just wanted a unique path

os.environ.setdefault("HERMES_AUTH_TOKEN", "test-token-for-pytest")
os.environ.setdefault("HERMES_LOG_LEVEL", "WARNING")
os.environ.setdefault("HERMES_DB_PATH", _TEST_DB_PATH)

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from hermes import config as hermes_config  # noqa: E402
from hermes.db import init_db  # noqa: E402


@pytest.fixture
async def conn(tmp_path: Path):
    """Yields an AsyncEngine bound to a fresh per-test SQLite DB.

    Fixture name stayed `conn` for diff-minimisation across the test
    suite during the SQLAlchemy refactor — repo functions take an engine
    now, so all callsites compile, just with a slightly misleading name.
    """
    engine = await init_db(str(tmp_path / "hermes.db"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_app_db_path(monkeypatch, tmp_path: Path) -> None:
    """Force each integration test (anything using LifespanManager) to boot
    against a fresh file-based DB. Without this the lifespan would re-use
    the module-level `_TEST_DB_PATH` between tests and leak state.
    """
    fresh = str(tmp_path / "hermes.db")
    monkeypatch.setattr(hermes_config.settings, "db_path", fresh)
