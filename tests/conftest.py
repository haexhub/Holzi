import os

os.environ.setdefault("HERMES_AUTH_TOKEN", "test-token-for-pytest")
os.environ.setdefault("HERMES_LOG_LEVEL", "WARNING")
os.environ.setdefault("HERMES_DB_PATH", ":memory:")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from hermes.db import init_db  # noqa: E402


@pytest.fixture
async def conn(tmp_path: Path):
    connection = await init_db(str(tmp_path / "hermes.db"))
    engine = connection.engine
    try:
        yield connection
    finally:
        await connection.close()
        # Dispose the engine so the aiosqlite worker thread stops before
        # pytest tears down the event loop — otherwise the worker tries
        # to call_soon_threadsafe on a closed loop and emits warnings.
        await engine.dispose()
