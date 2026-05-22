import time

import aiosqlite

from hermes.repository.models import Todo


def _row_to_todo(row: aiosqlite.Row) -> Todo:
    return Todo(
        id=row["id"],
        content=row["content"],
        tags=row["tags"],
        done_at=row["done_at"],
        created_at=row["created_at"],
    )


async def add(
    conn: aiosqlite.Connection,
    *,
    content: str,
    tags: str | None = None,
    ts: int | None = None,
) -> Todo:
    now = ts if ts is not None else int(time.time())
    cursor = await conn.execute(
        "INSERT INTO todos (content, tags, created_at) VALUES (?, ?, ?)",
        (content, tags, now),
    )
    await conn.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT into todos did not yield a rowid")
    return Todo(
        id=cursor.lastrowid,
        content=content,
        tags=tags,
        done_at=None,
        created_at=now,
    )


async def list_all(
    conn: aiosqlite.Connection,
    *,
    only_open: bool = True,
    tag: str | None = None,
    limit: int = 100,
) -> list[Todo]:
    clauses: list[str] = []
    params: list[object] = []
    if only_open:
        clauses.append("done_at IS NULL")
    if tag is not None:
        # Tags are stored comma-separated; match exact token in the list.
        clauses.append(
            "(',' || tags || ',') LIKE ?"
        )
        params.append(f"%,{tag},%")

    sql = "SELECT id, content, tags, done_at, created_at FROM todos"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    async with conn.execute(sql, tuple(params)) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_todo(r) for r in rows]


async def get(conn: aiosqlite.Connection, todo_id: int) -> Todo | None:
    async with conn.execute(
        "SELECT id, content, tags, done_at, created_at FROM todos WHERE id = ?",
        (todo_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return _row_to_todo(row) if row is not None else None


async def mark_done(
    conn: aiosqlite.Connection, todo_id: int, *, ts: int | None = None
) -> bool:
    now = ts if ts is not None else int(time.time())
    cursor = await conn.execute(
        "UPDATE todos SET done_at = ? WHERE id = ? AND done_at IS NULL",
        (now, todo_id),
    )
    await conn.commit()
    return cursor.rowcount > 0
