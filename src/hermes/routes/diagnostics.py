"""GET /api/diagnostics — redacted status snapshot for the Control Center.

Surfaces what a new user needs to set up before first chat (LLM credential,
workspace roots, sandbox runtime) plus the things that must be healthy at
runtime (database reachable, scheduler running). Plan 30 moved this away
from free-form `message` strings to `(code, params)` pairs so the frontend
can translate per locale. The overall status is the worst of the children
so the frontend can render a top-level badge without re-walking the list.

Redaction rule: this endpoint never returns API key plaintext, ciphertext
blob bytes, the master key, or the bearer auth token. Provider names,
display names, model ids, sandbox image tags and workspace root ids are
considered public — they land in `params` and the i18n template uses
them verbatim.
"""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.auth import current_user_id
from hermes.config import settings
from hermes.errors import ErrorCode
from hermes.logging import logger
from hermes.repository import llm_credentials as llm_repo
from hermes.repository import workspaces as workspaces_repo

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

Status = Literal["ok", "warning", "error"]
_SEVERITY: dict[Status, int] = {"ok": 0, "warning": 1, "error": 2}

# `params` is intentionally permissive: the only contract is "JSON-serialisable
# scalars and lists". The FE renders `errors.<code>` with these values
# interpolated. Anything user-controlled passes through `_summarise` first to
# keep the response bounded.
DiagParam = str | int | list[str]


class DiagnosticsCheck(BaseModel):
    id: str
    status: Status
    # Plan 30: the message string is gone; FE renders the localised
    # template under `errors.<code>` with `params` interpolated in.
    code: str
    params: dict[str, DiagParam] = {}


class DiagnosticsResponse(BaseModel):
    overall: Status
    checks: list[DiagnosticsCheck]


def _summarise(value: str, *, max_len: int) -> str:
    """Compact user-controlled free text into a single-line, length-capped
    form so the diagnostics `params` payload stays predictable."""
    one_line = " ".join(value.split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 1] + "…"


async def _check_database(db: AsyncEngine | None) -> DiagnosticsCheck:
    if db is None:
        return DiagnosticsCheck(
            id="database",
            status="error",
            code=ErrorCode.DIAG_DB_NOT_INITIALISED.value,
        )
    try:
        async with db.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface as degraded, don't 500
        logger.warning("diagnostics_db_error", error=str(exc))
        return DiagnosticsCheck(
            id="database",
            status="error",
            code=ErrorCode.DIAG_DB_UNREACHABLE.value,
        )
    return DiagnosticsCheck(
        id="database",
        status="ok",
        code=ErrorCode.DIAG_DB_REACHABLE.value,
    )


async def _check_llm(db: AsyncEngine, user_id: int) -> DiagnosticsCheck:
    active = await llm_repo.get_active(db, user_id=user_id)
    if active is None:
        return DiagnosticsCheck(
            id="llm",
            status="warning",
            code=ErrorCode.DIAG_LLM_NO_CREDENTIAL.value,
        )
    # display_name is user-controlled free text — truncate so an oversized
    # or multiline value can't dominate the response or push the badge
    # off-screen on the frontend.
    raw_display = active.display_name or active.provider
    display = _summarise(raw_display, max_len=48)
    model = active.model or settings.model
    return DiagnosticsCheck(
        id="llm",
        status="ok",
        code=ErrorCode.DIAG_LLM_ACTIVE.value,
        params={"display": display, "model": model},
    )


def _check_scheduler(request: Request) -> DiagnosticsCheck:
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return DiagnosticsCheck(
            id="scheduler",
            status="error",
            code=ErrorCode.DIAG_SCHEDULER_NOT_STARTED.value,
        )
    # Reaching into `_task` is deliberate: the scheduler manager survives
    # a crashed background loop (the asyncio.Task transitions to done()),
    # so `is not None` alone would silently report "ok" while no tasks fire.
    task = scheduler._task
    if task is None or task.done():
        return DiagnosticsCheck(
            id="scheduler",
            status="error",
            code=ErrorCode.DIAG_SCHEDULER_LOOP_STOPPED.value,
        )
    return DiagnosticsCheck(
        id="scheduler",
        status="ok",
        code=ErrorCode.DIAG_SCHEDULER_RUNNING.value,
    )


async def _check_workspace(db: AsyncEngine | None) -> DiagnosticsCheck:
    # Plan 25-A: `workspaces` table is the source of truth. The env stays
    # only as the boot-time backfill mechanism; nothing reads it at
    # request time anymore. A user who creates a workspace via
    # /settings/workspaces sees the warning clear without a restart.
    if db is None:
        return DiagnosticsCheck(
            id="workspace",
            status="error",
            code=ErrorCode.DIAG_WORKSPACE_NEEDS_DB.value,
        )
    rows = await workspaces_repo.list_active(db)
    if not rows:
        return DiagnosticsCheck(
            id="workspace",
            status="warning",
            code=ErrorCode.DIAG_WORKSPACE_NONE.value,
        )
    # `display_name` is user-controlled — apply the same single-line +
    # length-cap pass the LLM check uses so a runaway name can't dominate
    # the response. 48 chars per name is generous; first three + count
    # keeps the line short on big installs.
    preview_names = [_summarise(r.display_name, max_len=48) for r in rows[:3]]
    truncated = len(rows) > 3
    return DiagnosticsCheck(
        id="workspace",
        status="ok",
        code=ErrorCode.DIAG_WORKSPACE_CONFIGURED.value,
        params={
            "count": len(rows),
            "names": preview_names,
            "truncated": 1 if truncated else 0,
        },
    )


def _check_sandbox(request: Request) -> DiagnosticsCheck:
    manager = request.app.state.sandbox_manager
    if manager is not None:
        # "configured" rather than "ready" — manager creation succeeds at
        # boot but we don't ping the Podman socket here. A dead socket
        # surfaces lazily on the first sandbox spawn.
        return DiagnosticsCheck(
            id="sandbox",
            status="ok",
            code=ErrorCode.DIAG_SANDBOX_CONFIGURED.value,
            params={
                "image": settings.sandbox_image,
                "network": settings.sandbox_network,
            },
        )
    if not settings.sandbox_socket:
        return DiagnosticsCheck(
            id="sandbox",
            status="warning",
            code=ErrorCode.DIAG_SANDBOX_SOCKET_MISSING.value,
        )
    return DiagnosticsCheck(
        id="sandbox",
        status="error",
        code=ErrorCode.DIAG_SANDBOX_MANAGER_FAILED.value,
    )


@router.get("", response_model=DiagnosticsResponse)
async def api_diagnostics(request: Request) -> DiagnosticsResponse:
    db: AsyncEngine | None = request.app.state.db
    checks: list[DiagnosticsCheck] = [
        await _check_database(db),
    ]
    if db is not None:
        checks.append(await _check_llm(db, current_user_id(request)))
    else:
        checks.append(
            DiagnosticsCheck(
                id="llm",
                status="error",
                code=ErrorCode.DIAG_LLM_NEEDS_DB.value,
            )
        )
    checks.extend(
        [
            _check_scheduler(request),
            await _check_workspace(db),
            _check_sandbox(request),
        ]
    )
    overall: Status = "ok"
    for c in checks:
        if _SEVERITY[c.status] > _SEVERITY[overall]:
            overall = c.status
    return DiagnosticsResponse(overall=overall, checks=checks)
