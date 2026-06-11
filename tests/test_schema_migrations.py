from sqlalchemy import text


async def _column_names(db, table: str) -> set[str]:
    rows = (await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"
        ),
        {"t": table},
    )).all()
    return {r.column_name for r in rows}


async def test_users_has_identity_columns(conn) -> None:
    async with conn.connect() as db:
        cols = await _column_names(db, "users")
    assert {"email", "role", "parent_user_id"} <= cols


async def test_sessions_table_exists(conn) -> None:
    async with conn.connect() as db:
        cols = await _column_names(db, "sessions")
    assert {"id", "user_id", "token_hash", "label",
            "created_at", "last_used_at", "expires_at"} <= cols


async def test_users_role_defaults_member_on_fresh_insert(conn) -> None:
    async with conn.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, bootstrap_completed, created_at) "
                 "VALUES (2, false, 0)")
        )
    async with conn.connect() as db:
        role = (await db.execute(text("SELECT role FROM users WHERE id=2"))).scalar()
    assert role == "member"
