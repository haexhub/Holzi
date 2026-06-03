"""CRUD over registered external MCP servers (Plan 32).

CRUD writes are gated on the lifecycle manager: every successful
add / edit / delete / restart triggers a re-assembly of
`app.state.tool_catalog` so the next request observes the new tool set
(the MCP catalog endpoint itself reads `app.state.tool_catalog` live).

Secret bereinigung:
- POST / PUT accept `env` (raw map) and `credentials` (plaintext string)
  in the request body; the route encrypts and persists.
- GET responses NEVER include raw env values or plaintext credentials.
  `env` projects down to `env_keys` (variable names only); the
  ciphertext columns drop out entirely.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.crypto import Encryptor
from hermes.logging import logger
from hermes.mcp_manager import McpServerHandle, McpServerManager
from hermes.repository import mcp_servers as repo
from hermes.repository.models import McpServer
from hermes.tool_catalog import build_tool_catalog

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# Sentinel used to distinguish "field missing from PATCH" (leave as-is)
# from "field explicitly set to null" (clear it). Pydantic models with
# `default=None` can't tell those apart on their own; we detect them by
# inspecting the request body — see `_changed_keys`.
_UNSET = object()


# --- pydantic shapes -------------------------------------------------------


TransportLiteral = Literal["http", "stdio"]
StatusLiteral = Literal["starting", "ready", "crashed", "disabled", "unknown"]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")


class McpServerResponse(BaseModel):
    id: int
    name: str
    display_name: str
    transport: TransportLiteral
    url: str | None
    command_argv: list[str] | None
    env_keys: list[str]
    enabled: bool
    status: StatusLiteral
    last_error: str | None
    created_at: int
    updated_at: int


class McpServerListResponse(BaseModel):
    servers: list[McpServerResponse]
    total: int


class McpServerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=200)
    transport: TransportLiteral
    url: str | None = None
    command_argv: list[str] | None = None
    env: dict[str, str] | None = None
    credentials: str | None = None
    enabled: bool = True


class McpServerUpdate(BaseModel):
    """Partial update. Sentinel semantics (Plan 32 open-question note):
    a field omitted from the body is left as-is; a field set to `null`
    is cleared. The route reads the raw body to disambiguate."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = None
    command_argv: list[str] | None = None
    env: dict[str, str] | None = None
    credentials: str | None = None
    enabled: bool | None = None


class McpHealthSummary(BaseModel):
    """Per-server entry inside the aggregate /api/mcp/health response."""

    id: int
    name: str
    status: StatusLiteral
    last_error: str | None
    tool_count: int


class McpServerHealthResponse(BaseModel):
    id: int
    status: StatusLiteral
    last_error: str | None
    tool_count: int
    last_checked_at: float | None


# --- helpers ---------------------------------------------------------------


def _status_for(
    server: McpServer, handle: McpServerHandle | None
) -> StatusLiteral:
    """Project the manager's runtime status onto the response enum.

    A row that exists in the DB but has no handle (e.g. disabled, or
    boot-time start was skipped because the manager isn't around) is
    reported as "disabled" / "unknown" rather than crashing the response.
    """
    if not server.enabled:
        return "disabled"
    if handle is None:
        return "unknown"
    return handle.status


def _to_response(
    server: McpServer, handle: McpServerHandle | None
) -> McpServerResponse:
    return McpServerResponse(
        id=server.id,
        name=server.name,
        display_name=server.display_name,
        transport=server.transport,  # type: ignore[arg-type]
        url=server.url,
        command_argv=server.command_argv,
        env_keys=server.env_keys,
        enabled=server.enabled,
        status=_status_for(server, handle),
        last_error=(
            handle.last_error if handle is not None and handle.last_error else server.last_error
        ),
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _require_manager(request: Request) -> McpServerManager:
    manager: McpServerManager | None = getattr(
        request.app.state, "mcp_servers_manager", None
    )
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="MCP server manager not initialised",
        )
    return manager


async def _changed_keys(request: Request) -> set[str]:
    """Inspect the raw body so we can tell `null` from omitted on PUT."""
    try:
        body = await request.json()
    except (ValueError, RuntimeError):  # noqa: SIM105 — both empty + malformed
        return set()
    if not isinstance(body, dict):
        return set()
    return set(body.keys())


def _validate_create_shape(body: McpServerCreate) -> None:
    if not _SLUG_RE.fullmatch(body.name):
        raise HTTPException(
            status_code=422,
            detail=(
                "name must be kebab-case (a-z, 0-9, -), 2..32 chars, "
                "no leading/trailing dash"
            ),
        )
    if body.transport == "http":
        if not body.url:
            raise HTTPException(
                status_code=422, detail="http transport requires `url`"
            )
        if body.command_argv:
            raise HTTPException(
                status_code=422,
                detail="http transport must not set `command_argv`",
            )
        if body.env:
            raise HTTPException(
                status_code=422, detail="http transport must not set `env`"
            )
    else:  # stdio
        if not body.command_argv:
            raise HTTPException(
                status_code=422,
                detail="stdio transport requires `command_argv`",
            )
        if body.url:
            raise HTTPException(
                status_code=422, detail="stdio transport must not set `url`"
            )


# --- endpoints -------------------------------------------------------------


@router.get("/servers", response_model=McpServerListResponse)
async def list_servers(request: Request) -> McpServerListResponse:
    db: AsyncEngine = request.app.state.db
    manager: McpServerManager | None = getattr(
        request.app.state, "mcp_servers_manager", None
    )
    rows = await repo.list_all(db)
    items: list[McpServerResponse] = []
    for row in rows:
        handle = manager.get_handle(row.id) if manager is not None else None
        items.append(_to_response(row, handle))
    return McpServerListResponse(servers=items, total=len(items))


@router.post(
    "/servers",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_server(
    request: Request, body: McpServerCreate
) -> McpServerResponse:
    _validate_create_shape(body)
    db: AsyncEngine = request.app.state.db
    encryptor: Encryptor = request.app.state.encryptor
    manager = _require_manager(request)

    ciphertext = encryptor.encrypt(body.credentials) if body.credentials else None
    try:
        row = await repo.create(
            db,
            name=body.name,
            display_name=body.display_name,
            transport=body.transport,
            url=body.url,
            command_argv=body.command_argv,
            env=body.env,
            ciphertext=ciphertext,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"mcp server name {body.name!r} already exists"
        ) from exc

    handle: McpServerHandle | None = None
    if body.enabled:
        try:
            handle = await manager.start_server(row.id)
        except Exception as exc:  # noqa: BLE001 — surface to user
            logger.warning(
                "mcp_create_start_failed", server=row.name, error=str(exc)
            )
    _refresh_catalog(request)
    return _to_response(row, handle)


@router.put(
    "/servers/{server_id}", response_model=McpServerResponse
)
async def update_server(
    request: Request, server_id: int, body: McpServerUpdate
) -> McpServerResponse:
    db: AsyncEngine = request.app.state.db
    encryptor: Encryptor = request.app.state.encryptor
    manager = _require_manager(request)

    existing = await repo.get(db, server_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="mcp server not found")

    keys = await _changed_keys(request)

    # Credential update semantics, mirroring the plan's PUT sentinel:
    #   - field omitted → leave as-is
    #   - field == null → clear (clear_credentials=True)
    #   - field == "..."  → encrypt + set
    ciphertext = None
    clear_credentials = False
    if "credentials" in keys:
        if body.credentials is None:
            clear_credentials = True
        else:
            ciphertext = encryptor.encrypt(body.credentials)

    # URL / argv / env: same omitted-vs-null disambiguation. Pass kwargs
    # only when the client included the field; otherwise the repo's
    # _UNSET sentinel leaves the column alone.
    extra: dict[str, Any] = {}
    if "url" in keys:
        extra["url"] = body.url
    if "command_argv" in keys:
        extra["command_argv"] = body.command_argv
    if "env" in keys:
        extra["env"] = body.env

    try:
        updated = await repo.update(
            db,
            server_id,
            display_name=body.display_name,
            ciphertext=ciphertext,
            clear_credentials=clear_credentials,
            enabled=body.enabled,
            **extra,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="mcp server not found")

    # Anything that materially affects the connection triggers a restart.
    restart_triggers = {"url", "command_argv", "env", "credentials"}
    needs_restart = (
        bool(keys & restart_triggers)
        or (body.enabled is not None and body.enabled != existing.enabled)
    )
    handle: McpServerHandle | None = None
    if needs_restart:
        if updated.enabled:
            try:
                handle = await manager.restart_server(server_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "mcp_update_restart_failed",
                    server=updated.name,
                    error=str(exc),
                )
        else:
            await manager.stop_server(server_id)
    else:
        handle = manager.get_handle(server_id)
    _refresh_catalog(request)
    return _to_response(updated, handle)


@router.delete(
    "/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_server(request: Request, server_id: int) -> Response:
    db: AsyncEngine = request.app.state.db
    manager = _require_manager(request)
    existing = await repo.get(db, server_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    await manager.stop_server(server_id)
    if not await repo.delete(db, server_id):
        raise HTTPException(status_code=404, detail="mcp server not found")
    _refresh_catalog(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/servers/{server_id}/restart", response_model=McpServerResponse
)
async def restart_server(
    request: Request, server_id: int
) -> McpServerResponse:
    db: AsyncEngine = request.app.state.db
    manager = _require_manager(request)
    row = await repo.get(db, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    handle: McpServerHandle | None = None
    try:
        handle = await manager.restart_server(server_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mcp_manual_restart_failed", server=row.name, error=str(exc)
        )
    _refresh_catalog(request)
    return _to_response(row, handle)


@router.get(
    "/servers/{server_id}/health", response_model=McpServerHealthResponse
)
async def server_health(
    request: Request, server_id: int
) -> McpServerHealthResponse:
    db: AsyncEngine = request.app.state.db
    manager: McpServerManager | None = getattr(
        request.app.state, "mcp_servers_manager", None
    )
    row = await repo.get(db, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    handle = manager.get_handle(server_id) if manager is not None else None
    status_value = _status_for(row, handle)
    tool_count = len(handle.tools) if handle is not None else 0
    last_error = (
        handle.last_error if handle is not None and handle.last_error else row.last_error
    )
    return McpServerHealthResponse(
        id=row.id,
        status=status_value,
        last_error=last_error,
        tool_count=tool_count,
        last_checked_at=handle.last_checked_at if handle is not None else None,
    )


# --- catalog refresh -------------------------------------------------------


def _refresh_catalog(request: Request) -> None:
    """Re-assemble `app.state.tool_catalog` so the next request observes
    the new MCP tool set. Single-worker invariant — no locking required.
    """
    state = request.app.state
    state.tool_catalog = build_tool_catalog(
        db=state.db,
        signal_client=state.signal_client,
        signal_self_number=state.signal_self_number,
        external_http=state.external_http,
        brave_api_key=state.brave_api_key,
        mcp_manager=state.mcp_servers_manager,
        current_channel=None,
    )
