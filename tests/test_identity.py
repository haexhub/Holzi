from sqlalchemy import text

from hermes.identity import Identity, SessionResolver, hash_token


def test_hash_token_is_stable_sha256_hex() -> None:
    h = hash_token("abc")
    assert h == hash_token("abc")
    assert len(h) == 64 and h != "abc"


async def test_resolver_returns_identity_for_active_session(conn) -> None:
    token = "secret-token-xyz"
    async with conn.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, role, bootstrap_completed, created_at) "
                 "VALUES (7, 'member', 0, 0)")
        )
        await db.execute(
            text("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) "
                 "VALUES (7, :h, 0, NULL)"),
            {"h": hash_token(token)},
        )
    ident = await SessionResolver(conn).resolve(token)
    assert ident == Identity(user_id=7, role="member")


async def test_resolver_rejects_expired_session(conn) -> None:
    token = "expired-xyz"
    async with conn.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, role, bootstrap_completed, created_at) "
                 "VALUES (8, 'member', 0, 0)")
        )
        await db.execute(
            text("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) "
                 "VALUES (8, :h, 0, 1)"),
            {"h": hash_token(token)},
        )
    assert await SessionResolver(conn).resolve(token) is None


async def test_resolver_returns_none_for_unknown_token(conn) -> None:
    assert await SessionResolver(conn).resolve("nope") is None
