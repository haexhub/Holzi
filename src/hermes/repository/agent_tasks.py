"""Persistence layer for `agent_tasks` (Plan 16).

A row is either one-shot (`due_at` set, `schedule` NULL) or recurring
(`schedule` set, `due_at` is the *next* computed firing). The scheduler
picks rows where `enabled = 1 AND due_at <= now()`; after a run it
rolls `due_at` forward via the cron expression for recurring tasks, or
flips `enabled` to 0 for one-shot.

`last_run_id` is a loose pointer at `agent_runs.id` — there's no FK
because that direction would close a cycle SQLite can't model (see
`schema.py`). A stale id is harmless; the UI falls back gracefully.
"""
import time

from croniter import croniter
from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository.models import AgentTask
from hermes.schema import agent_tasks as t_agent_tasks


def _row_to_task(row) -> AgentTask:
    return AgentTask(
        id=row.id,
        title=row.title,
        prompt=row.prompt,
        due_at=row.due_at,
        schedule=row.schedule,
        timezone=row.timezone,
        enabled=bool(row.enabled),
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        last_run_id=row.last_run_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def validate_schedule(schedule: str) -> None:
    """Raise `ValueError` if `schedule` is not a valid 5-field cron expression."""
    if not croniter.is_valid(schedule):
        raise ValueError(f"invalid cron expression: {schedule!r}")


def next_fire_after(schedule: str, *, after: int, timezone: str = "UTC") -> int:
    """Return the next epoch second a cron expression should fire after `after`.

    Wraps croniter so the rest of the codebase doesn't import it directly —
    the scheduler and the API both need this and shouldn't drift on tz
    handling.
    """
    import datetime
    import zoneinfo

    tz = zoneinfo.ZoneInfo(timezone)
    base = datetime.datetime.fromtimestamp(after, tz=tz)
    itr = croniter(schedule, base)
    next_dt = itr.get_next(datetime.datetime)
    return int(next_dt.timestamp())


async def create(
    engine: AsyncEngine,
    *,
    title: str,
    prompt: str,
    due_at: int | None = None,
    schedule: str | None = None,
    timezone: str = "UTC",
    enabled: bool = True,
    ts: int | None = None,
) -> AgentTask:
    """Create a row. Caller must supply exactly one of `due_at` / `schedule`."""
    if (due_at is None) == (schedule is None):
        raise ValueError("exactly one of due_at / schedule must be set")
    if schedule is not None:
        validate_schedule(schedule)

    now = ts if ts is not None else int(time.time())
    # For recurring tasks, materialise the first firing into `due_at` so the
    # scheduler's "enabled AND due_at <= now" query stays cheap (no cron eval
    # in the hot path). The invariant check above guarantees schedule is set
    # when due_at is None — narrow it explicitly for the type checker.
    if due_at is not None:
        effective_due_at = due_at
    else:
        assert schedule is not None
        effective_due_at = next_fire_after(schedule, after=now, timezone=timezone)
    async with engine.begin() as conn:
        result = await conn.execute(
            t_agent_tasks.insert()
            .values(
                title=title,
                prompt=prompt,
                due_at=effective_due_at,
                schedule=schedule,
                timezone=timezone,
                enabled=1 if enabled else 0,
                created_at=now,
                updated_at=now,
            )
            .returning(t_agent_tasks.c.id)
        )
        row = result.first()
    if row is None:
        raise RuntimeError("INSERT into agent_tasks did not yield a rowid")
    return AgentTask(
        id=row.id,
        title=title,
        prompt=prompt,
        due_at=effective_due_at,
        schedule=schedule,
        timezone=timezone,
        enabled=enabled,
        last_run_at=None,
        last_status=None,
        last_run_id=None,
        created_at=now,
        updated_at=now,
    )


async def get(engine: AsyncEngine, task_id: int) -> AgentTask | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_agent_tasks).where(t_agent_tasks.c.id == task_id)
        )
        row = result.first()
    return _row_to_task(row) if row is not None else None


async def list_all(
    engine: AsyncEngine, *, limit: int = 200
) -> list[AgentTask]:
    """Stable listing for the settings UI: nearest due first, then by title."""
    stmt = (
        select(t_agent_tasks)
        .order_by(
            asc(t_agent_tasks.c.due_at),
            asc(t_agent_tasks.c.title),
        )
        .limit(limit)
    )
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_task(r) for r in rows]


async def list_due(engine: AsyncEngine, *, now: int) -> list[AgentTask]:
    """Scheduler hot path: enabled rows whose next firing has arrived."""
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_agent_tasks)
            .where(t_agent_tasks.c.enabled == 1)
            .where(t_agent_tasks.c.due_at.is_not(None))
            .where(t_agent_tasks.c.due_at <= now)
            .order_by(asc(t_agent_tasks.c.due_at))
        )
        rows = result.all()
    return [_row_to_task(r) for r in rows]


async def update(
    engine: AsyncEngine,
    task_id: int,
    *,
    title: str | None = None,
    prompt: str | None = None,
    due_at: int | None = None,
    schedule: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    ts: int | None = None,
    # Sentinel for "clear this nullable field" — `None` already means
    # "don't touch", so callers need a way to set due_at/schedule to NULL
    # explicitly when switching a task between one-shot and recurring.
    clear_due_at: bool = False,
    clear_schedule: bool = False,
) -> AgentTask | None:
    """Patch a task. Caller must keep the "exactly one of due_at/schedule"
    invariant; this enforces it after applying the patch."""
    existing = await get(engine, task_id)
    if existing is None:
        return None

    new_title = title if title is not None else existing.title
    new_prompt = prompt if prompt is not None else existing.prompt
    new_timezone = timezone if timezone is not None else existing.timezone

    new_due_at: int | None
    if clear_due_at:
        new_due_at = None
    elif due_at is not None:
        new_due_at = due_at
    else:
        new_due_at = existing.due_at

    new_schedule: str | None
    if clear_schedule:
        new_schedule = None
    elif schedule is not None:
        new_schedule = schedule
    else:
        new_schedule = existing.schedule

    if new_schedule is not None:
        validate_schedule(new_schedule)

    # Materialise the next firing when we have a recurring task and either
    # (a) due_at is empty (caller cleared it or switched from one-shot to
    # recurring) or (b) the rule itself changed (schedule/timezone) so the
    # cached due_at would be stale. Without this the next tick would fire
    # on the previous schedule until the next mark_run recompute.
    now = ts if ts is not None else int(time.time())
    if new_schedule is not None and (
        new_due_at is None
        or schedule is not None
        or timezone is not None
    ):
        new_due_at = next_fire_after(new_schedule, after=now, timezone=new_timezone)

    # A row must always have a due_at to be schedulable. Recurring rows
    # carry both (schedule = rule, due_at = cached next firing); one-shot
    # rows have due_at only. The only invalid state is "nothing to fire".
    if new_due_at is None:
        raise ValueError("task must have either due_at or schedule")

    new_enabled = existing.enabled if enabled is None else enabled

    async with engine.begin() as conn:
        await conn.execute(
            t_agent_tasks.update()
            .where(t_agent_tasks.c.id == task_id)
            .values(
                title=new_title,
                prompt=new_prompt,
                due_at=new_due_at,
                schedule=new_schedule,
                timezone=new_timezone,
                enabled=1 if new_enabled else 0,
                updated_at=now,
            )
        )
    return await get(engine, task_id)


async def delete(engine: AsyncEngine, task_id: int) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(
            t_agent_tasks.delete().where(t_agent_tasks.c.id == task_id)
        )
    return (result.rowcount or 0) > 0


async def mark_run(
    engine: AsyncEngine,
    task_id: int,
    *,
    run_id: str,
    status: str,
    ts: int | None = None,
) -> AgentTask | None:
    """Record one firing of `task_id`.

    For recurring tasks (schedule != NULL) advances `due_at` to the next cron
    occurrence; for one-shot tasks (due_at != NULL, schedule == NULL) flips
    `enabled` to 0 so the row stops being picked up by `list_due`. The row
    itself stays around so the user can still see its history in the UI.
    """
    existing = await get(engine, task_id)
    if existing is None:
        return None

    now = ts if ts is not None else int(time.time())
    values: dict = {
        "last_run_at": now,
        "last_status": status,
        "last_run_id": run_id,
        "updated_at": now,
    }
    if existing.schedule is not None:
        values["due_at"] = next_fire_after(
            existing.schedule, after=now, timezone=existing.timezone
        )
    else:
        values["enabled"] = 0

    async with engine.begin() as conn:
        await conn.execute(
            t_agent_tasks.update()
            .where(t_agent_tasks.c.id == task_id)
            .values(**values)
        )
    return await get(engine, task_id)
