"""Workspace sandbox status + restart (Plan 11b-b).

Exposes the SandboxManager so the UI can show liveness and offer the
Restart action behind the `sandbox_crashed` event. The sandbox manager is
only present when the agent is configured with a Podman socket; without it
these endpoints return 503 so the caller can fall back gracefully."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from hermes.routes._helpers import require_sandbox_manager

router = APIRouter()


class SandboxStatusResponse(BaseModel):
    workspace_id: str
    state: Literal["running", "exited", "crashed", "oom", "removed", "absent"]
    exit_code: int | None = None


@router.get(
    "/workspaces/{workspace_id}/sandbox",
    response_model=SandboxStatusResponse,
)
async def api_get_sandbox_status(
    request: Request, workspace_id: str
) -> dict[str, Any]:
    mgr = require_sandbox_manager(request)
    handle = mgr.peek_workspace(workspace_id)
    if handle is None:
        # No sandbox has been spun up for this workspace yet. "absent" is
        # distinct from "removed" (which means we *had* one and it's gone).
        return {"workspace_id": workspace_id, "state": "absent", "exit_code": None}
    status_value = await mgr.status(handle)
    return {
        "workspace_id": workspace_id,
        "state": status_value.state.value,
        "exit_code": status_value.exit_code,
    }


@router.post(
    "/workspaces/{workspace_id}/sandbox/restart",
    response_model=SandboxStatusResponse,
)
async def api_restart_sandbox(
    request: Request, workspace_id: str
) -> dict[str, Any]:
    mgr = require_sandbox_manager(request)
    handle = await mgr.restart_workspace(workspace_id)
    status_value = await mgr.status(handle)
    return {
        "workspace_id": workspace_id,
        "state": status_value.state.value,
        "exit_code": status_value.exit_code,
    }
