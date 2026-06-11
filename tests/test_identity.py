from sqlalchemy import text

from hermes.identity import Identity, SessionResolver, hash_token


def test_hash_token_is_stable_sha256_hex() -> None:
    h = hash_token("abc")
    assert h == hash_token("abc")
    assert len(h) == 64 and h != "abc"


async def test_resolver_returns_identity_for_active_session(owner_engine) -> None:
    # `users` + `sessions` are seeded via the owner engine (bypasses RLS);
    # the per-request resolver also runs against the owner engine (see main.py).
    token = "secret-token-xyz"
    async with owner_engine.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, role, bootstrap_completed, created_at) "
                 "VALUES (7, 'member', false, 0)")
        )
        await db.execute(
            text("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) "
                 "VALUES (7, :h, 0, NULL)"),
            {"h": hash_token(token)},
        )
    ident = await SessionResolver(owner_engine).resolve(token)
    assert ident == Identity(user_id=7, role="member")


async def test_resolver_rejects_expired_session(owner_engine) -> None:
    token = "expired-xyz"
    async with owner_engine.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, role, bootstrap_completed, created_at) "
                 "VALUES (8, 'member', false, 0)")
        )
        await db.execute(
            text("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) "
                 "VALUES (8, :h, 0, 1)"),
            {"h": hash_token(token)},
        )
    assert await SessionResolver(owner_engine).resolve(token) is None


async def test_resolver_resolves_far_future_session(owner_engine) -> None:
    token = "future-xyz"
    async with owner_engine.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, role, bootstrap_completed, created_at) "
                 "VALUES (9, 'member', false, 0)")
        )
        # `expires_at` is a 32-bit INTEGER column; 2_000_000_000 (year 2033)
        # is comfortably in the future yet within range.
        await db.execute(
            text("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) "
                 "VALUES (9, :h, 0, 2000000000)"),
            {"h": hash_token(token)},
        )
    ident = await SessionResolver(owner_engine).resolve(token)
    assert ident == Identity(user_id=9, role="member")


async def test_resolver_returns_none_for_unknown_token(owner_engine) -> None:
    assert await SessionResolver(owner_engine).resolve("nope") is None
