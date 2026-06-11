"""Persistence layer for the `agent_runs` table.

Each /api/chat (and signal/telegram) turn writes exactly one row through
`insert` + `finalize`. The web layer mirrors the run_id into
`app.state.chat_runs` so the cancel endpoint can flip the in-process
event; the table itself is the source of truth for history, error
context, and the GET /api/runs listing.

Status transitions are intentionally one-way:
    insert -> 'running'
    finalize -> 'success' | 'cancelled' | 'error'
A row whose state is 'running' but whose process has died is recoverable
only by an out-of-band sweep; this layer never resurrects rows.
"""
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.db import tx_for_user
from hermes.repository.models import AgentRun
from hermes.schema import agent_runs as t_agent_runs

VALID_STATUSES: frozenset[str] = frozenset(
    {"running", "success", "cancelled", "error"}
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"success", "cancelled", "error"})


def _row_to_agent_run(row) -> AgentRun:
    return AgentRun(
        id=row.id,
        conversation_id=row.conversation_id,
        channel=row.channel,
        model=row.model,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        error_code=row.error_code,
        error_message=row.error_message,
        error_trace=row.error_trace,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        agent_task_id=row.agent_task_id,
    )


async def insert(
    engine: AsyncEngine,
    *,
    user_id: int,
    run_id: str,
    conversation_id: int,
    channel: str,
    model: str,
    started_at: int,
    agent_task_id: int | None = None,
) -> None:
    """Create the row with status='running'. Caller is expected to follow
    up with `finalize` in a `try/finally` so the row never lingers in
    'running' across a clean process exit.

    `agent_task_id` is set by the scheduler when a run was triggered by an
    `agent_tasks` row; plain /api/chat runs leave it NULL. `user_id` is
    denormalised from the parent conversation (Plan §1).
    """
    async with tx_for_user(engine, user_id=user_id) as conn:
        await conn.execute(
            t_agent_runs.insert().values(
                id=run_id,
                conversation_id=conversation_id,
                channel=channel,
                model=model,
                started_at=started_at,
                finished_at=None,
                status="running",
                agent_task_id=agent_task_id,
                user_id=user_id,
            )
        )


async def finalize(
    engine: AsyncEngine,
    run_id: str,
    *,
    user_id: int,
    status: str,
    finished_at: int,
    error_code: str | None = None,
    error_message: str | None = None,
    error_trace: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Move the row to one of the terminal statuses. Idempotent on
    no-such-id (the row was already swept / never existed); callers can
    invoke this from a generic finally block without checking first.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"finalize() requires a terminal status, got {status!r}")
    values: dict = {"status": status, "finished_at": finished_at}
    if error_code is not None:
        values["error_code"] = error_code
    if error_message is not None:
        values["error_message"] = error_message
    if error_trace is not None:
        values["error_trace"] = error_trace
    if input_tokens is not None:
        values["input_tokens"] = input_tokens
    if output_tokens is not None:
        values["output_tokens"] = output_tokens
    async with tx_for_user(engine, user_id=user_id) as conn:
        # Guard on status='running' so a repeated or concurrent finalize
        # can't overwrite an already-terminal row and clobber its history.
        await conn.execute(
            t_agent_runs.update()
            .where(
                t_agent_runs.c.id == run_id,
                t_agent_runs.c.status == "running",
            )
            .values(**values)
        )


async def get(engine: AsyncEngine, run_id: str, *, user_id: int) -> AgentRun | None:
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(
            select(t_agent_runs).where(t_agent_runs.c.id == run_id)
        )
        row = result.first()
    return _row_to_agent_run(row) if row is not None else None


async def list_runs(
    engine: AsyncEngine,
    *,
    user_id: int,
    conversation_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AgentRun]:
    """Newest-first listing for the diagnostics endpoint."""
    stmt = select(t_agent_runs)
    if conversation_id is not None:
        stmt = stmt.where(t_agent_runs.c.conversation_id == conversation_id)
    if status is not None:
        stmt = stmt.where(t_agent_runs.c.status == status)
    stmt = stmt.order_by(desc(t_agent_runs.c.started_at)).limit(limit).offset(offset)
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_agent_run(r) for r in rows]


# ---------------------------------------------------------------------------
# Aggregates (Plan 27 — /api/insights)
# ---------------------------------------------------------------------------


async def aggregate_totals(
    engine: AsyncEngine, *, user_id: int, since_ts: int
) -> dict[str, int]:
    """Sum runs / errors / tokens for rows with `started_at >= since_ts`."""
    stmt = select(
        func.count(t_agent_runs.c.id).label("runs"),
        func.coalesce(func.sum(t_agent_runs.c.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(t_agent_runs.c.output_tokens), 0).label("output_tokens"),
        func.coalesce(
            func.sum(
                (t_agent_runs.c.status == "error").cast(t_agent_runs.c.id.type)
            ),
            0,
        ).label("errors"),
    ).where(t_agent_runs.c.started_at >= since_ts)
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        # Aggregate over COUNT/SUM/coalesce always produces exactly one row.
        row = result.one()
    return {
        "runs": int(row.runs or 0),
        "errors": int(row.errors or 0),
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
    }


async def aggregate_by_day(
    engine: AsyncEngine, *, user_id: int, since_ts: int
) -> list[dict[str, int | str]]:
    """Group rows into UTC date buckets. Returns raw (non-zero-filled) rows;
    the route layer fills the rest of the window with zero buckets so the
    chart never lies about empty days."""
    bucket = func.strftime("%Y-%m-%d", t_agent_runs.c.started_at, "unixepoch")
    stmt = (
        select(
            bucket.label("bucket"),
            func.count(t_agent_runs.c.id).label("runs"),
            func.coalesce(func.sum(t_agent_runs.c.input_tokens), 0).label(
                "input_tokens"
            ),
            func.coalesce(func.sum(t_agent_runs.c.output_tokens), 0).label(
                "output_tokens"
            ),
        )
        .where(t_agent_runs.c.started_at >= since_ts)
        .group_by(bucket)
        .order_by(bucket)
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [
        {
            "bucket": r.bucket,
            "runs": int(r.runs or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
        }
        for r in rows
    ]


async def aggregate_by_model(
    engine: AsyncEngine, *, user_id: int, since_ts: int
) -> list[dict[str, int | str]]:
    """Per-model totals over the same window."""
    stmt = (
        select(
            t_agent_runs.c.model.label("model"),
            func.count(t_agent_runs.c.id).label("runs"),
            func.coalesce(func.sum(t_agent_runs.c.input_tokens), 0).label(
                "input_tokens"
            ),
            func.coalesce(func.sum(t_agent_runs.c.output_tokens), 0).label(
                "output_tokens"
            ),
            func.coalesce(
                func.sum(
                    (t_agent_runs.c.status == "error").cast(t_agent_runs.c.id.type)
                ),
                0,
            ).label("errors"),
        )
        .where(t_agent_runs.c.started_at >= since_ts)
        .group_by(t_agent_runs.c.model)
        .order_by(desc("runs"))
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [
        {
            "model": r.model,
            "runs": int(r.runs or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "errors": int(r.errors or 0),
        }
        for r in rows
    ]


async def aggregate_by_status(
    engine: AsyncEngine, *, user_id: int, since_ts: int
) -> dict[str, int]:
    """Counts per status value. Zero-fills every VALID_STATUSES key so the
    frontend doesn't have to handle missing keys."""
    # Label as "n" rather than "count" — Row exposes labels as attributes and
    # `Row.count` is already a built-in method, so the access shadows the data.
    stmt = (
        select(
            t_agent_runs.c.status.label("status"),
            func.count(t_agent_runs.c.id).label("n"),
        )
        .where(t_agent_runs.c.started_at >= since_ts)
        .group_by(t_agent_runs.c.status)
    )
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    counts = {s: 0 for s in VALID_STATUSES}
    for r in rows:
        if r.status in counts:
            counts[r.status] = int(r.n or 0)
    return counts
