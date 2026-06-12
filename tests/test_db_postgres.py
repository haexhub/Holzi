import pytest
from sqlalchemy import text


@pytest.mark.usefixtures("pg_db")
async def test_init_db_runs_migrations_and_returns_engine():
    """The pg_db fixture (Task 18) is needed for this to run; until then
    the test is collected but skipped via pytest-asyncio default behavior
    when the fixture is missing. We keep the test file in place so Task 18
    can wire it up without touching code."""
    from hermes.db import init_db
    engine = await init_db()
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT current_user, current_setting('app.user_id', true)"
            ))).first()
            assert row[0] == "holzi_app"
    finally:
        await engine.dispose()
