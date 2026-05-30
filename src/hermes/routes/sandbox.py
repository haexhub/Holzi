"""GET /api/sandbox/crashes — persistent sandbox-crash history (Plan 20-A).

Backs the "Sandbox-Abstürze" section on /settings/diagnostics. Read-only;
the only writer is the lifespan-registered crash handler in `main.py`
that subscribes to `SandboxManager.add_crash_handler` so every
`WorkspaceCrash` the health watcher fires lands in the DB even when no
chat stream is connected.

Returns newest-first. `limit` is clamped via FastAPI's Query validator
so a runaway caller can't ask for the entire table at once.

# Cross-system invariant — `state`

The `state` field is typed as `Literal["crashed", "oom", "removed"]`
on this Pydantic model so the regenerated frontend types stay
exhaustive (the frontend's `CRASH_STATE_LABEL` map uses
`satisfies Record<SandboxCrashState, string>` to enforce the same
finite set). The canonical writer is `_DEAD_STATES` in
`hermes/sandbox/manager.py`. Adding a new SandboxState to the dead
set is a five-step change — update `_DEAD_STATES`, update the
Literal below, regenerate the frontend types (`pnpm run gen:api`),
extend the frontend map, and add the German label. If `_DEAD_STATES`
ever grows and this Literal does not, the response will 500 on the
new row; that is the deliberate failure mode so the gap is loud
rather than silently rendering "unknown" in the UI.
"""
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import sandbox_crashes as repo

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


class SandboxCrashResponse(BaseModel):
    """One row from `sandbox_crashes`. See module docstring for the
    cross-system invariant on the `state` field."""

    id: int
    workspace_id: str
    sandbox_id: str
    crashed_at: int = Field(
        ...,
        description=(
            "Unix epoch seconds when the health watcher's handler fired "
            "for this dead-transition."
        ),
    )
    state: Literal["crashed", "oom", "removed"] = Field(
        ...,
        description=(
            "SandboxState value the watcher reported — see `_DEAD_STATES` "
            "in `sandbox/manager.py` for the canonical writer set."
        ),
    )
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
            # `state` is the DB-stored string. The Literal above narrows
            # against the canonical writer set; see module docstring.
            state=r.state,  # type: ignore[arg-type]
            exit_code=r.exit_code,
            last_message=r.last_message,
        )
        for r in rows
    ]
