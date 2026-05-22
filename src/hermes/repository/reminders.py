import time

import aiosqlite

from hermes.repository.models import Reminder


def _row_to_reminder(row: aiosqlite.Row) -> Reminder:
    return Reminder(
        id=row["id"],
        due_at=row["due_at"],
        message=row["message"],
        channel=row["channel"],
        fired_at=row["fired_at"],
        created_at=row["created_at"],
    )


async def create(
    conn: aiosqlite.Connection,
    *,
    due_at: int,
    message: str,
    channel: str = "signal",
    ts: int | None = None,
) -> Reminder:
    now = ts if ts is not None else int(time.time())
    cursor = await conn.execute(
        "INSERT INTO reminders (due_at, message, channel, created_at) "
        "VALUES (?, ?, ?, ?)",
        (due_at, message, channel, now),
    )
    await conn.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("INSERT into reminders did not yield a rowid")
    return Reminder(
        id=cursor.lastrowid,
        due_at=due_at,
        message=message,
        channel=channel,
        fired_at=None,
        created_at=now,
    )


async def list_all(
    conn: aiosqlite.Connection, *, include_fired: bool = False, limit: int = 50
) -> list[Reminder]:
    if include_fired:
        sql = (
            "SELECT id, due_at, message, channel, fired_at, created_at "
            "FROM reminders ORDER BY due_at ASC LIMIT ?"
        )
        params: tuple = (limit,)
    else:
        sql = (
            "SELECT id, due_at, message, channel, fired_at, created_at "
            "FROM reminders WHERE fired_at IS NULL ORDER BY due_at ASC LIMIT ?"
        )
        params = (limit,)
    async with conn.execute(sql, params) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_reminder(r) for r in rows]


async def list_due(conn: aiosqlite.Connection, *, now: int) -> list[Reminder]:
    async with conn.execute(
        "SELECT id, due_at, message, channel, fired_at, created_at "
        "FROM reminders WHERE fired_at IS NULL AND due_at <= ? "
        "ORDER BY due_at ASC",
        (now,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_reminder(r) for r in rows]


async def mark_fired(
    conn: aiosqlite.Connection, reminder_id: int, *, ts: int | None = None
) -> None:
    now = ts if ts is not None else int(time.time())
    await conn.execute(
        "UPDATE reminders SET fired_at = ? WHERE id = ? AND fired_at IS NULL",
        (now, reminder_id),
    )
    await conn.commit()
