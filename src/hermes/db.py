from importlib.resources import files

import aiosqlite

SCHEMA_SQL = files("hermes").joinpath("schema.sql").read_text(encoding="utf-8")


async def init_db(path: str) -> aiosqlite.Connection:
    """Open an aiosqlite connection, apply schema, return it.

    Re-running against an existing DB is safe — schema.sql uses
    `IF NOT EXISTS` everywhere.
    """
    conn = await aiosqlite.connect(path)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
    except BaseException:
        await conn.close()
        raise
    return conn
