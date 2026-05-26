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
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncEngine

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
    )


async def insert(
    engine: AsyncEngine,
    *,
    run_id: str,
    conversation_id: int,
    channel: str,
    model: str,
    started_at: int,
) -> None:
    """Create the row with status='running'. Caller is expected to follow
    up with `finalize` in a `try/finally` so the row never lingers in
    'running' across a clean process exit."""
    async with engine.begin() as conn:
        await conn.execute(
            t_agent_runs.insert().values(
                id=run_id,
                conversation_id=conversation_id,
                channel=channel,
                model=model,
                started_at=started_at,
                finished_at=None,
                status="running",
            )
        )


async def finalize(
    engine: AsyncEngine,
    run_id: str,
    *,
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
    async with engine.begin() as conn:
        await conn.execute(
            t_agent_runs.update()
            .where(t_agent_runs.c.id == run_id)
            .values(**values)
        )


async def get(engine: AsyncEngine, run_id: str) -> AgentRun | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(t_agent_runs).where(t_agent_runs.c.id == run_id)
        )
        row = result.first()
    return _row_to_agent_run(row) if row is not None else None


async def list_runs(
    engine: AsyncEngine,
    *,
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
    async with engine.connect() as conn:
        result = await conn.execute(stmt)
        rows = result.all()
    return [_row_to_agent_run(r) for r in rows]
