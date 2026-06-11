"""Database bootstrap.

Owns the single `AsyncEngine` for the app. Consumers (route handlers,
scheduler tick) open their own short-lived `AsyncConnection` via
`engine.begin()` so that each logical operation sits in its own
transaction. Sharing a single long-lived connection across concurrent
tasks is unsafe per SQLAlchemy's ownership model — two coroutines
committing on the same connection would race.

Schema lives in two places:
- `schema.py` — SQLAlchemy Core `Table` definitions for the regular
  tables. Applied via `metadata.create_all()`.
- `schema.sql` — SQLite-specific bits SQLAlchemy doesn't model: FTS5
  virtual tables and their sync triggers. Applied as raw SQL after the
  metadata create.
"""
from importlib.resources import files

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from hermes.schema import metadata

_FTS_SCHEMA_SQL = files("hermes").joinpath("schema.sql").read_text(encoding="utf-8")


@event.listens_for(Engine, "connect")
def _sqlite_set_pragmas(dbapi_connection, _record) -> None:
    """Apply per-connection SQLite PRAGMAs on every pool checkout.

    `PRAGMA foreign_keys` is per-connection, not per-database — without
    this every new connection from the pool would silently disable FK
    enforcement and lose the integrity guarantees the schema relies on.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


async def init_db(path: str) -> AsyncEngine:
    """Open an `AsyncEngine`, apply schema, return it.

    Re-running against an existing DB is safe — `metadata.create_all`
    issues `CREATE TABLE IF NOT EXISTS` and the FTS5 schema is also
    idempotent.

    Callers acquire connections via `async with engine.begin() as conn:`
    (auto-commits on success, rolls back on exception) for writes, or
    `engine.connect()` for read-only operations.
    """
    if path == ":memory:":
        # `:memory:` databases are per-connection by default — every new
        # checkout from the pool would open a fresh empty DB. StaticPool
        # plus check_same_thread=False keeps a single shared in-memory DB
        # alive for the lifetime of the engine. **Warning**: StaticPool
        # serialises every operation through one connection, so any
        # concurrent caller (e.g. the agent-task scheduler running in
        # parallel with a request) will race on transaction state. Use
        # file-based paths for anything beyond toy/scripts; the test
        # suite explicitly switches to tmp_path SQLite files for this
        # reason.
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as conn:
            if path != ":memory:":
                # journal_mode is per-DB and persists in the file; one-time
                # set is enough. foreign_keys is per-connection — handled
                # by the global connect listener above.
                await conn.execute(text("PRAGMA journal_mode = WAL"))
            await conn.run_sync(metadata.create_all)
            await _apply_lightweight_migrations(conn)
            for stmt in _split_statements(_FTS_SCHEMA_SQL):
                await conn.execute(text(stmt))
    except BaseException:
        await engine.dispose()
        raise
    return engine


async def _apply_lightweight_migrations(conn) -> None:
    """Apply additive column adds that `metadata.create_all` won't touch
    on an existing table. SQLite has no `ADD COLUMN IF NOT EXISTS`, so
    we check `PRAGMA table_info` first.

    Keep this list short — anything non-trivial deserves a proper
    migration tool (Alembic) instead of growing this function.
    """
    cols = await conn.execute(text("PRAGMA table_info(llm_credentials)"))
    existing = {row[1] for row in cols.all()}
    if "model" not in existing:
        await conn.execute(text("ALTER TABLE llm_credentials ADD COLUMN model TEXT"))

    cols = await conn.execute(text("PRAGMA table_info(conversations)"))
    existing = {row[1] for row in cols.all()}
    if "bookmarked" not in existing:
        await conn.execute(
            text(
                "ALTER TABLE conversations ADD COLUMN bookmarked "
                "INTEGER NOT NULL DEFAULT 0"
            )
        )
    if "expires_at" not in existing:
        await conn.execute(
            text("ALTER TABLE conversations ADD COLUMN expires_at INTEGER")
        )
        # Backfill `expires_at` for rows that existed before this migration —
        # otherwise the sweep skips every legacy conversation (it filters
        # `expires_at IS NOT NULL`) and the feature is silently inert on
        # already-deployed DBs. Import-locally to avoid an import cycle
        # between db.py and config.py.
        from hermes.config import settings

        await conn.execute(
            text(
                "UPDATE conversations "
                "SET expires_at = updated_at + :window "
                "WHERE expires_at IS NULL AND bookmarked = 0"
            ),
            {"window": settings.conversation_ttl_days * 86_400},
        )
    # Plan 35 §C1: scope conversations by owning user. On a pre-C1 DB the
    # column is missing (create_all won't ALTER an existing table); add it and
    # backfill every legacy row to the seeded admin (id=1). The per-user index
    # lives here (not in schema.py) so it's created AFTER the column exists on
    # both fresh and existing DBs — see schema.py for the rationale.
    if "user_id" not in existing:
        await conn.execute(
            text("ALTER TABLE conversations ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS conv_user_updated "
            "ON conversations(user_id, updated_at DESC)"
        )
    )

    # Plan 35 §C1: scope notes (the agent's memory store) by owning user.
    # Same shape as conversations above — on a pre-C1 DB the column is
    # missing (create_all won't ALTER an existing table); add it and backfill
    # every legacy row to the seeded admin (id=1). The per-user index lives
    # here (not in schema.py) so it's created AFTER the column exists on both
    # fresh and existing DBs. We deliberately do NOT touch the old global
    # `notes.key` unique index on existing DBs — SQLite can't easily drop it
    # and for single-user C1 a stricter global-unique key is harmless.
    cols = await conn.execute(text("PRAGMA table_info(notes)"))
    notes_cols = {row[1] for row in cols.all()}
    if "user_id" not in notes_cols:
        await conn.execute(
            text("ALTER TABLE notes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS notes_user ON notes(user_id, updated_at DESC)")
    )

    # Plan 16: agent_tasks replaces reminders + todos. Drop the legacy tables
    # on upgrade so re-running metadata.create_all() doesn't recreate them
    # via leftover SQLAlchemy references in old code paths. The data was
    # bot-internal scratch state (Signal pings + the agent's todo list) —
    # acceptable loss in exchange for one canonical scheduled-task concept.
    await conn.execute(text("DROP INDEX IF EXISTS reminders_due_pending"))
    await conn.execute(text("DROP TABLE IF EXISTS reminders"))
    await conn.execute(text("DROP TABLE IF EXISTS todos"))

    # Plan 34: messenger surface removed. Drop the messenger_accounts table
    # and prune any leftover conversations/messages/attachments tied to
    # the deprecated 'signal' / 'telegram' channels. FK cascades take care
    # of children (messages, attachments, agent_runs).
    await conn.execute(
        text("DROP INDEX IF EXISTS messenger_accounts_active_per_provider")
    )
    await conn.execute(text("DROP TABLE IF EXISTS messenger_accounts"))
    await conn.execute(
        text(
            "DELETE FROM conversations WHERE channel IN ('signal', 'telegram')"
        )
    )
    await conn.execute(
        text(
            "DELETE FROM channel_prompts WHERE channel IN ('signal', 'telegram')"
        )
    )

    cols = await conn.execute(text("PRAGMA table_info(agent_runs)"))
    existing = {row[1] for row in cols.all()}
    if "agent_task_id" not in existing:
        await conn.execute(
            text("ALTER TABLE agent_runs ADD COLUMN agent_task_id INTEGER")
        )

    # Plan 35 §C1: extend the minimal `users` table with identity columns on
    # existing DBs. The `sessions` table + its index are auto-created by
    # metadata.create_all, so only these ALTERs need handling here.
    cols = await conn.execute(text("PRAGMA table_info(users)"))
    existing = {row[1] for row in cols.all()}
    if "email" not in existing:
        await conn.execute(text("ALTER TABLE users ADD COLUMN email TEXT"))
    if "role" not in existing:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        )
        # The pre-existing single-user seed row (id=1) becomes the admin.
        await conn.execute(text("UPDATE users SET role = 'admin' WHERE id = 1"))
    if "parent_user_id" not in existing:
        await conn.execute(text("ALTER TABLE users ADD COLUMN parent_user_id INTEGER"))


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements, respecting BEGIN/END
    blocks (which contain semicolons of their own — used by FTS5 triggers).
    """
    statements: list[str] = []
    buf: list[str] = []
    in_block = False
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buf.append(line)
        upper = line.upper()
        if "BEGIN" in upper and not in_block:
            in_block = True
            continue
        if in_block:
            if upper.startswith("END;"):
                in_block = False
                statements.append(" ".join(buf))
                buf = []
            continue
        if line.endswith(";"):
            statements.append(" ".join(buf))
            buf = []
    if buf:
        statements.append(" ".join(buf))
    return [s.rstrip(";").strip() for s in statements if s.strip(";").strip()]
