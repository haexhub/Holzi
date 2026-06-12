import pytest
from sqlalchemy import text

from hermes.config import settings
from hermes.identity import hash_token
from hermes.users import ensure_platform_admin_seeded


@pytest.mark.usefixtures("pg_db")
async def test_platform_admin_seeded_from_env(owner_engine, monkeypatch):
    """First boot creates the admin row + a never-expiring session keyed
    on HERMES_PLATFORM_ADMIN_TOKEN."""
    # Mutate the imported config singleton in place — ensure_platform_admin_seeded
    # reads `settings.platform_admin_*` off this object. importlib.reload would
    # rebind a NEW Settings instance that hermes.users never sees.
    monkeypatch.setattr(settings, "platform_admin_email", "admin@example.com")
    monkeypatch.setattr(settings, "platform_admin_token", "rotated-token")

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
    monkeypatch.setattr(settings, "platform_admin_email", "admin@example.com")
    monkeypatch.setattr(settings, "platform_admin_token", "old-token")

    await ensure_platform_admin_seeded(owner_engine)

    monkeypatch.setattr(settings, "platform_admin_token", "new-token")
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
