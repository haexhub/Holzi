"""GET /api/sandbox/crashes — persistent sandbox-crash history (Plan 20-A).

Backs the "Sandbox-Abstürze" section on /settings/diagnostics. Read-only;
the only writer is the lifespan-registered crash handler in `main.py`
that subscribes to `SandboxManager.add_crash_handler` so every
`WorkspaceCrash` the health watcher fires lands in the DB even when no
chat stream is connected.

Returns newest-first. `limit` is clamped via FastAPI's Query validator
so a runaway caller can't ask for the entire table at once.
"""
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import sandbox_crashes as repo

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


class SandboxCrashResponse(BaseModel):
    """One row from `sandbox_crashes`. `state` mirrors the
    `SandboxState` enum the watcher reported — kept as a literal so the
    frontend gets exhaustive type coverage if a new state appears."""

    id: int
    workspace_id: str
    sandbox_id: str
    crashed_at: int
    state: Literal["crashed", "oom", "removed"]
    exit_code: int | None
    last_message: str | None


@router.get("/crashes", response_model=list[SandboxCrashResponse])
async def api_sandbox_crashes(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[SandboxCrashResponse]:
    db: AsyncEngine = request.app.state.db
    rows = await repo.list_recent(db, limit=limit)
    return [
        SandboxCrashResponse(
            id=r.id,
            workspace_id=r.workspace_id,
            sandbox_id=r.sandbox_id,
            crashed_at=r.crashed_at,
            # The state column is constrained at write time (only the
            # manager's enum values reach `insert`), so this cast is safe.
            state=r.state,  # type: ignore[arg-type]
            exit_code=r.exit_code,
            last_message=r.last_message,
        )
        for r in rows
    ]
