import time

from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.repository.models import Todo
from hermes.schema import todos as t_todos


def _row_to_todo(row) -> Todo:
    return Todo(
        id=row.id,
        content=row.content,
        tags=row.tags,
        done_at=row.done_at,
        created_at=row.created_at,
    )


async def add(
    conn: AsyncConnection,
    *,
    content: str,
    tags: str | None = None,
    ts: int | None = None,
) -> Todo:
    now = ts if ts is not None else int(time.time())
    result = await conn.execute(
        t_todos.insert()
        .values(content=content, tags=tags, created_at=now)
        .returning(t_todos.c.id)
    )
    await conn.commit()
    row = result.first()
    if row is None:
        raise RuntimeError("INSERT into todos did not yield a rowid")
    return Todo(
        id=row.id,
        content=content,
        tags=tags,
        done_at=None,
        created_at=now,
    )


async def list_all(
    conn: AsyncConnection,
    *,
    only_open: bool = True,
    tag: str | None = None,
    limit: int = 100,
) -> list[Todo]:
    stmt = select(t_todos)
    if only_open:
        stmt = stmt.where(t_todos.c.done_at.is_(None))
    if tag is not None:
        # Tags stored comma-separated; match exact token in the list. The
        # SQLite || concatenation isn't a first-class Core op, so do the
        # comma-bounded LIKE via raw text with a bound parameter.
        stmt = stmt.where(
            text("(',' || todos.tags || ',') LIKE :tag_pattern")
        ).params(tag_pattern=f"%,{tag},%")
    stmt = stmt.order_by(desc(t_todos.c.created_at)).limit(limit)

    result = await conn.execute(stmt)
    return [_row_to_todo(r) for r in result]


async def get(conn: AsyncConnection, todo_id: int) -> Todo | None:
    result = await conn.execute(select(t_todos).where(t_todos.c.id == todo_id))
    row = result.first()
    return _row_to_todo(row) if row is not None else None


async def mark_done(
    conn: AsyncConnection, todo_id: int, *, ts: int | None = None
) -> bool:
    now = ts if ts is not None else int(time.time())
    result = await conn.execute(
        t_todos.update()
        .where(t_todos.c.id == todo_id)
        .where(t_todos.c.done_at.is_(None))
        .values(done_at=now)
    )
    await conn.commit()
    return (result.rowcount or 0) > 0


async def delete(conn: AsyncConnection, todo_id: int) -> bool:
    result = await conn.execute(t_todos.delete().where(t_todos.c.id == todo_id))
    await conn.commit()
    return (result.rowcount or 0) > 0
