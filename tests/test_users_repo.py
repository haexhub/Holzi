"""Tests for hermes.users — ensure_platform_admin_seeded + is_bootstrap_completed.

Runs against the OWNER engine (bypasses RLS) — the lifespan seeds the
platform_admin this way, since at boot there is no resolved user yet.
"""
import pytest
from sqlalchemy import text

from hermes.config import settings
from hermes.identity import hash_token
from hermes.users import (
    BOOTSTRAP_LABEL,
    ensure_platform_admin_seeded,
    is_bootstrap_completed,
)


@pytest.mark.asyncio
async def test_ensure_seeded_inserts_row(owner_engine):
    """First call inserts a single row with id=1, bootstrap_completed=false."""
    uid = await ensure_platform_admin_seeded(owner_engine)
    assert uid == 1
    async with owner_engine.connect() as db:
        row = (
            await db.execute(text("SELECT id, bootstrap_completed FROM users WHERE id=1"))
        ).first()
    assert row is not None
    assert row.id == 1
    assert row.bootstrap_completed is False


@pytest.mark.asyncio
async def test_ensure_seeded_idempotent(owner_engine):
    """Second call is a no-op — no duplicate rows, no error."""
    await ensure_platform_admin_seeded(owner_engine)
    await ensure_platform_admin_seeded(owner_engine)
    async with owner_engine.connect() as db:
        count = (
            await db.execute(text("SELECT COUNT(*) FROM users"))
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_is_bootstrap_completed_false_initially(owner_engine):
    """Returns False on a fresh seed (bootstrap_completed=false)."""
    uid = await ensure_platform_admin_seeded(owner_engine)
    result = await is_bootstrap_completed(owner_engine, uid)
    assert result is False


@pytest.mark.asyncio
async def test_is_bootstrap_completed_true_after_flip(owner_engine):
    """Returns True after bootstrap_completed is set to true."""
    uid = await ensure_platform_admin_seeded(owner_engine)
    async with owner_engine.begin() as db:
        await db.execute(
            text("UPDATE users SET bootstrap_completed = true WHERE id = :uid"),
            {"uid": uid},
        )
    result = await is_bootstrap_completed(owner_engine, uid)
    assert result is True


@pytest.mark.asyncio
async def test_is_bootstrap_completed_false_when_missing(owner_engine):
    """Returns False defensively when the row doesn't exist yet."""
    # Don't seed — table is empty
    result = await is_bootstrap_completed(owner_engine, 1)
    assert result is False


@pytest.mark.asyncio
async def test_seed_creates_admin_bootstrap_session(owner_engine) -> None:
    await ensure_platform_admin_seeded(owner_engine)
    async with owner_engine.connect() as db:
        role = (await db.execute(text("SELECT role FROM users WHERE id=1"))).scalar()
        sess = (await db.execute(text(
            "SELECT user_id, token_hash, expires_at FROM sessions"
        ))).first()
    assert role == "platform_admin"
    assert sess.user_id == 1
    assert sess.token_hash == hash_token(settings.platform_admin_token)
    assert sess.expires_at is None   # never expires


@pytest.mark.asyncio
async def test_seed_is_idempotent_no_duplicate_session(owner_engine) -> None:
    await ensure_platform_admin_seeded(owner_engine)
    await ensure_platform_admin_seeded(owner_engine)  # second boot
    async with owner_engine.connect() as db:
        count = (await db.execute(text("SELECT COUNT(*) FROM sessions"))).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_seed_replaces_stale_bootstrap_session_on_rotation(owner_engine, monkeypatch) -> None:
    await ensure_platform_admin_seeded(owner_engine)
    # rotate the env token, re-seed
    monkeypatch.setattr(settings, "platform_admin_token", "rotated-token-value")
    await ensure_platform_admin_seeded(owner_engine)
    async with owner_engine.connect() as db:
        rows = (await db.execute(
            text("SELECT token_hash FROM sessions WHERE label = :l"),
            {"l": BOOTSTRAP_LABEL},
        )).all()
    hashes = {r.token_hash for r in rows}
    assert hashes == {hash_token("rotated-token-value")}  # old one gone, only new
