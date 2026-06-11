"""Tests for hermes.users — ensure_users_seeded + is_bootstrap_completed."""
import pytest
from sqlalchemy import text

from hermes.config import settings
from hermes.identity import hash_token
from hermes.users import ensure_users_seeded, is_bootstrap_completed


@pytest.mark.asyncio
async def test_ensure_users_seeded_inserts_row(conn):
    """First call inserts a single row with id=1, bootstrap_completed=0."""
    await ensure_users_seeded(conn)
    async with conn.connect() as db:
        row = (
            await db.execute(text("SELECT id, bootstrap_completed FROM users WHERE id=1"))
        ).first()
    assert row is not None
    assert row.id == 1
    assert row.bootstrap_completed == 0


@pytest.mark.asyncio
async def test_ensure_users_seeded_idempotent(conn):
    """Second call is a no-op — no duplicate rows, no error."""
    await ensure_users_seeded(conn)
    await ensure_users_seeded(conn)
    async with conn.connect() as db:
        count = (
            await db.execute(text("SELECT COUNT(*) FROM users"))
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_is_bootstrap_completed_false_initially(conn):
    """Returns False on a fresh seed (bootstrap_completed=0)."""
    await ensure_users_seeded(conn)
    result = await is_bootstrap_completed(conn)
    assert result is False


@pytest.mark.asyncio
async def test_is_bootstrap_completed_true_after_flip(conn):
    """Returns True after bootstrap_completed is set to 1."""
    await ensure_users_seeded(conn)
    async with conn.begin() as db:
        await db.execute(
            text("UPDATE users SET bootstrap_completed = 1 WHERE id = 1")
        )
    result = await is_bootstrap_completed(conn)
    assert result is True


@pytest.mark.asyncio
async def test_is_bootstrap_completed_false_when_missing(conn):
    """Returns False defensively when the row doesn't exist yet."""
    # Don't seed — table is empty
    result = await is_bootstrap_completed(conn)
    assert result is False


@pytest.mark.asyncio
async def test_seed_creates_admin_bootstrap_session(conn) -> None:
    await ensure_users_seeded(conn)
    async with conn.connect() as db:
        role = (await db.execute(text("SELECT role FROM users WHERE id=1"))).scalar()
        sess = (await db.execute(text(
            "SELECT user_id, token_hash, expires_at FROM sessions"
        ))).first()
    assert role == "admin"
    assert sess.user_id == 1
    assert sess.token_hash == hash_token(settings.auth_token)
    assert sess.expires_at is None   # never expires (operator's own machine)


@pytest.mark.asyncio
async def test_seed_is_idempotent_no_duplicate_session(conn) -> None:
    await ensure_users_seeded(conn)
    await ensure_users_seeded(conn)  # second boot
    async with conn.connect() as db:
        count = (await db.execute(text("SELECT COUNT(*) FROM sessions"))).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_seed_replaces_stale_bootstrap_session_on_rotation(conn, monkeypatch) -> None:
    await ensure_users_seeded(conn)
    # rotate the env token, re-seed
    import hermes.config as cfg
    monkeypatch.setattr(cfg.settings, "auth_token", "rotated-token-value")
    await ensure_users_seeded(conn)
    from hermes.identity import hash_token
    async with conn.connect() as db:
        rows = (await db.execute(text(
            "SELECT token_hash FROM sessions WHERE label='bootstrap admin'"
        ))).all()
    hashes = {r.token_hash for r in rows}
    assert hashes == {hash_token("rotated-token-value")}  # old one gone, only new
