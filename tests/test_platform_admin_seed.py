import importlib

import pytest
from sqlalchemy import text

from hermes.identity import hash_token


@pytest.mark.usefixtures("pg_db")
async def test_platform_admin_seeded_from_env(owner_engine, monkeypatch):
    """First boot creates the admin row + a never-expiring session keyed
    on HERMES_PLATFORM_ADMIN_TOKEN."""
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "rotated-token")
    import hermes.config
    importlib.reload(hermes.config)

    from hermes.users import ensure_platform_admin_seeded
    await ensure_platform_admin_seeded(owner_engine)

    async with owner_engine.connect() as conn:
        u = (await conn.execute(text(
            "SELECT id, email, role FROM users WHERE email='admin@example.com'"
        ))).first()
        assert u is not None
        assert u.role == "platform_admin"

        s = (await conn.execute(
            text("SELECT user_id FROM sessions WHERE token_hash=:h"),
            {"h": hash_token("rotated-token")},
        )).first()
        assert s is not None
        assert s.user_id == u.id


@pytest.mark.usefixtures("pg_db")
async def test_admin_token_rotation_drops_stale_session(owner_engine, monkeypatch):
    """Rotating HERMES_PLATFORM_ADMIN_TOKEN drops the previous bootstrap
    session so the old token stops working."""
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "old-token")
    import hermes.config
    importlib.reload(hermes.config)

    from hermes.users import ensure_platform_admin_seeded
    await ensure_platform_admin_seeded(owner_engine)

    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "new-token")
    importlib.reload(hermes.config)
    await ensure_platform_admin_seeded(owner_engine)

    async with owner_engine.connect() as conn:
        # New token works
        new = (await conn.execute(
            text("SELECT user_id FROM sessions WHERE token_hash=:h"),
            {"h": hash_token("new-token")},
        )).first()
        assert new is not None
        # Old token's bootstrap session was dropped (only the bootstrap-labeled
        # one; other sessions with the same role would be unaffected, but here
        # we know we only seeded one).
        old = (await conn.execute(
            text("SELECT user_id FROM sessions WHERE token_hash=:h"),
            {"h": hash_token("old-token")},
        )).first()
        assert old is None
