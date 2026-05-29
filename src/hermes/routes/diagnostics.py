"""GET /api/diagnostics — redacted status snapshot for the Control Center.

Surfaces what a new user needs to set up before first chat (LLM credential,
messenger account, workspace roots, sandbox runtime) plus the things that
must be healthy at runtime (database reachable, scheduler running). Every
check returns a short human-readable message; the overall status is the
worst of the children so the frontend can render a top-level badge without
re-walking the list.

Redaction rule: this endpoint never returns API key plaintext, ciphertext
blob bytes, the master key, the bearer auth token, or messenger account
identifiers (phone numbers). Provider names, display names, model ids,
sandbox image tags and workspace root ids are considered public.
"""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.config import settings
from hermes.logging import logger
from hermes.repository import llm_credentials as llm_repo
from hermes.repository import messenger as messenger_repo

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

Status = Literal["ok", "warning", "error"]
_SEVERITY: dict[Status, int] = {"ok": 0, "warning": 1, "error": 2}


class DiagnosticsCheck(BaseModel):
    id: str
    label: str
    status: Status
    message: str


class DiagnosticsResponse(BaseModel):
    overall: Status
    checks: list[DiagnosticsCheck]


async def _check_database(db: AsyncEngine | None) -> DiagnosticsCheck:
    if db is None:
        return DiagnosticsCheck(
            id="database",
            label="Database",
            status="error",
            message="engine not initialised",
        )
    try:
        async with db.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — surface as degraded, don't 500
        logger.warning("diagnostics_db_error", error=str(exc))
        return DiagnosticsCheck(
            id="database",
            label="Database",
            status="error",
            message="unreachable",
        )
    return DiagnosticsCheck(
        id="database", label="Database", status="ok", message="reachable"
    )


async def _check_llm(db: AsyncEngine) -> DiagnosticsCheck:
    active = await llm_repo.get_active(db)
    if active is None:
        return DiagnosticsCheck(
            id="llm",
            label="LLM",
            status="warning",
            message="no active LLM credential — chat will fail until one is configured",
        )
    display = active.display_name or active.provider
    model = active.model or settings.model
    return DiagnosticsCheck(
        id="llm",
        label="LLM",
        status="ok",
        message=f"active credential: {display} (model {model})",
    )


async def _check_messenger(db: AsyncEngine) -> DiagnosticsCheck:
    accounts = await messenger_repo.list_all(db)
    active = [a for a in accounts if a.is_active]
    if not active:
        return DiagnosticsCheck(
            id="messenger",
            label="Messenger",
            status="warning",
            message="no active messenger account — Signal/Telegram bridges are off",
        )
    providers = sorted({a.provider for a in active})
    return DiagnosticsCheck(
        id="messenger",
        label="Messenger",
        status="ok",
        message=f"{len(active)} active account(s): {', '.join(providers)}",
    )


def _check_scheduler(request: Request) -> DiagnosticsCheck:
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return DiagnosticsCheck(
            id="scheduler",
            label="Scheduler",
            status="error",
            message="agent task scheduler did not start",
        )
    return DiagnosticsCheck(
        id="scheduler",
        label="Scheduler",
        status="ok",
        message="agent task scheduler is running",
    )


def _check_workspace() -> DiagnosticsCheck:
    roots = [r.strip() for r in settings.workspace_roots.split(",") if r.strip()]
    if not roots:
        return DiagnosticsCheck(
            id="workspace",
            label="Workspaces",
            status="warning",
            message="no workspace roots configured (HERMES_WORKSPACE_ROOTS empty)",
        )
    return DiagnosticsCheck(
        id="workspace",
        label="Workspaces",
        status="ok",
        message=f"{len(roots)} root(s) configured: {', '.join(roots)}",
    )


def _check_sandbox(request: Request) -> DiagnosticsCheck:
    manager = request.app.state.sandbox_manager
    if manager is not None:
        return DiagnosticsCheck(
            id="sandbox",
            label="Sandbox runtime",
            status="ok",
            message=(
                f"rootless Podman ready "
                f"(image {settings.sandbox_image}, network {settings.sandbox_network})"
            ),
        )
    if not settings.sandbox_socket:
        return DiagnosticsCheck(
            id="sandbox",
            label="Sandbox runtime",
            status="warning",
            message="HERMES_SANDBOX_SOCKET not set — tool calls that need a sandbox will fail",
        )
    return DiagnosticsCheck(
        id="sandbox",
        label="Sandbox runtime",
        status="error",
        message="sandbox socket configured but manager failed to start",
    )


@router.get("", response_model=DiagnosticsResponse)
async def api_diagnostics(request: Request) -> DiagnosticsResponse:
    db: AsyncEngine | None = request.app.state.db
    checks: list[DiagnosticsCheck] = [
        await _check_database(db),
    ]
    if db is not None:
        checks.append(await _check_llm(db))
        checks.append(await _check_messenger(db))
    else:
        checks.append(
            DiagnosticsCheck(
                id="llm",
                label="LLM",
                status="error",
                message="cannot check — database not initialised",
            )
        )
        checks.append(
            DiagnosticsCheck(
                id="messenger",
                label="Messenger",
                status="error",
                message="cannot check — database not initialised",
            )
        )
    checks.extend(
        [
            _check_scheduler(request),
            _check_workspace(),
            _check_sandbox(request),
        ]
    )
    overall: Status = "ok"
    for c in checks:
        if _SEVERITY[c.status] > _SEVERITY[overall]:
            overall = c.status
    return DiagnosticsResponse(overall=overall, checks=checks)
