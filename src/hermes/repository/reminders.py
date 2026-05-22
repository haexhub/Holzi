import time

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from hermes.repository.models import Reminder
from hermes.schema import reminders as t_reminders


def _row_to_reminder(row) -> Reminder:
    return Reminder(
        id=row.id,
        due_at=row.due_at,
        message=row.message,
        channel=row.channel,
        fired_at=row.fired_at,
        created_at=row.created_at,
    )


async def create(
    conn: AsyncConnection,
    *,
    due_at: int,
    message: str,
    channel: str = "signal",
    ts: int | None = None,
) -> Reminder:
    now = ts if ts is not None else int(time.time())
    result = await conn.execute(
        t_reminders.insert()
        .values(due_at=due_at, message=message, channel=channel, created_at=now)
        .returning(t_reminders.c.id)
    )
    await conn.commit()
    row = result.first()
    if row is None:
        raise RuntimeError("INSERT into reminders did not yield a rowid")
    return Reminder(
        id=row.id,
        due_at=due_at,
        message=message,
        channel=channel,
        fired_at=None,
        created_at=now,
    )


async def list_all(
    conn: AsyncConnection, *, include_fired: bool = False, limit: int = 50
) -> list[Reminder]:
    stmt = select(t_reminders)
    if not include_fired:
        stmt = stmt.where(t_reminders.c.fired_at.is_(None))
    stmt = stmt.order_by(asc(t_reminders.c.due_at)).limit(limit)
    result = await conn.execute(stmt)
    return [_row_to_reminder(r) for r in result]


async def list_due(conn: AsyncConnection, *, now: int) -> list[Reminder]:
    result = await conn.execute(
        select(t_reminders)
        .where(t_reminders.c.fired_at.is_(None))
        .where(t_reminders.c.due_at <= now)
        .order_by(asc(t_reminders.c.due_at))
    )
    return [_row_to_reminder(r) for r in result]


async def delete(conn: AsyncConnection, reminder_id: int) -> bool:
    result = await conn.execute(
        t_reminders.delete().where(t_reminders.c.id == reminder_id)
    )
    await conn.commit()
    return (result.rowcount or 0) > 0


async def mark_fired(
    conn: AsyncConnection, reminder_id: int, *, ts: int | None = None
) -> None:
    now = ts if ts is not None else int(time.time())
    await conn.execute(
        t_reminders.update()
        .where(t_reminders.c.id == reminder_id)
        .where(t_reminders.c.fired_at.is_(None))
        .values(fired_at=now)
    )
    await conn.commit()
