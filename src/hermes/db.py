"""Database bootstrap.

Owns the single long-lived `AsyncConnection` that the rest of the app
uses for queries. We keep a single connection (not a pool) because the
app is single-user with low concurrency — pool overhead would buy
nothing.

Schema lives in two places:
- `schema.py` — SQLAlchemy Core `Table` definitions for the regular
  tables (conversations, messages, notes, reminders, todos). Applied via
  `metadata.create_all()`.
- `schema.sql` — SQLite-specific bits SQLAlchemy doesn't model: FTS5
  virtual tables and the triggers that keep them in sync with the
  content tables. Applied as raw SQL after the metadata create.
"""
from importlib.resources import files

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from hermes.schema import metadata

# Read raw FTS5 schema (virtual tables + triggers + the partial index for
# pending reminders) — these are SQLite-specific and not modelled in
# `schema.py`.
_FTS_SCHEMA_SQL = files("hermes").joinpath("schema.sql").read_text(encoding="utf-8")


async def init_db(path: str) -> AsyncConnection:
    """Open an AsyncConnection, apply schema, return it.

    Re-running against an existing DB is safe — `metadata.create_all`
    issues `CREATE TABLE IF NOT EXISTS` and the FTS5 schema in
    `schema.sql` is also idempotent.
    """
    url = (
        "sqlite+aiosqlite:///:memory:"
        if path == ":memory:"
        else f"sqlite+aiosqlite:///{path}"
    )
    engine = create_async_engine(url)
    conn = await engine.connect()
    try:
        await conn.execute(text("PRAGMA foreign_keys = ON"))
        if path != ":memory:":
            await conn.execute(text("PRAGMA journal_mode = WAL"))
        # Managed-table DDL via SQLAlchemy.
        await conn.run_sync(metadata.create_all)
        # FTS5 virtual tables + triggers via raw SQL.
        for stmt in _split_statements(_FTS_SCHEMA_SQL):
            await conn.execute(text(stmt))
        await conn.commit()
    except BaseException:
        await conn.close()
        await engine.dispose()
        raise
    # SQLAlchemy's connection holds a reference to the engine via
    # `conn.sync_engine`; close() will dispose, so we don't need to track
    # the engine separately.
    return conn


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements, respecting BEGIN/END
    blocks (which contain semicolons of their own — used by FTS5 triggers).

    The parser is intentionally tiny: it only handles what `schema.sql`
    actually contains. Strip comments and blank lines, then track whether
    we're inside a `BEGIN ... END;` block.
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
    # Drop the trailing semicolon since SQLAlchemy's `text()` doesn't need it.
    return [s.rstrip(";").strip() for s in statements if s.strip(";").strip()]
