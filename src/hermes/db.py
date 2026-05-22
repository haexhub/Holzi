"""Database bootstrap.

Owns the single `AsyncEngine` for the app. Consumers (route handlers,
scheduler tick, signal worker) open their own short-lived
`AsyncConnection` via `engine.begin()` so that each logical operation
sits in its own transaction. Sharing a single long-lived connection
across concurrent tasks is unsafe per SQLAlchemy's ownership model —
two coroutines committing on the same connection would race.

Schema lives in two places:
- `schema.py` — SQLAlchemy Core `Table` definitions for the regular
  tables. Applied via `metadata.create_all()`.
- `schema.sql` — SQLite-specific bits SQLAlchemy doesn't model: FTS5
  virtual tables, sync triggers, the partial reminders index. Applied
  as raw SQL after the metadata create.
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
        # concurrent caller (e.g. the reminder scheduler running in
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
            for stmt in _split_statements(_FTS_SCHEMA_SQL):
                await conn.execute(text(stmt))
    except BaseException:
        await engine.dispose()
        raise
    return engine


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
