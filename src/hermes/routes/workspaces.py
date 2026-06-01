"""Multi-workspace CRUD (Plan 25).

Workspaces become a first-class managed object: create / rename / archive
via the UI without env edits or container restarts. The sandbox runtime,
git status, and disk usage are joined into the list response so the
`/settings/workspaces` page renders one round-trip.

The slug is the only user-controlled value that ends up in a path
(`${sandbox_volume_root}/${id}`); it's validated tightly at the repository
layer and 400s a bad slug here.

Adjacent endpoints (existing, deliberately *not* moved here):
- `GET /api/workspace/{roots,tree,file,git}` — Plan 12/13/24 browser,
  write and git surface in `routes/workspace.py` (singular). Also reads
  from this table at request time (Plan 25-A); the env is bootstrap-only.
- `GET /api/workspaces/{id}/sandbox` + `POST .../sandbox/restart` —
  Plan 11b-b sandbox lifecycle in `routes/api.py`. The aggregate `GET
  /api/workspaces` here reuses the same `peek_workspace`/`status` calls.
"""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import workspaces as repo
from hermes.sandbox import (
    ExecExit,
    ExecOutput,
    SandboxError,
    SandboxHandle,
    SandboxManager,
    SandboxNotRunning,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

# Workspace volume mount inside the sandbox container — same constant as
# `routes/workspace.py`, duplicated to keep the import surface tiny.
_WORKSPACE_MOUNT = "/workspace"

# `du -sb` should answer in well under a second; cap at one second so a
# wedged sandbox can't stall the workspaces-list response.
_DISK_TIMEOUT = 1.0


# --- response / request models ---------------------------------------------


SandboxStateValue = Literal[
    "absent", "running", "exited", "crashed", "oom", "removed"
]


class WorkspaceSandbox(BaseModel):
    """Aggregated sandbox snapshot for the workspace list view.

    `state="absent"` means no handle is cached for this workspace (no chat
    has touched it since the agent started); other values mirror
    `SandboxState` from `sandbox/manager.py`. `exit_code` is null unless
    the container has exited or crashed.
    """

    state: SandboxStateValue
    exit_code: int | None = None


class WorkspaceDisk(BaseModel):
    """Disk-usage snapshot. Null when the probe didn't return cleanly —
    sandbox absent, crashed, timeout, or `du` non-zero exit. The page
    renders "—" rather than misreporting a stale number."""

    used_mb: int | None = None
    quota_mb: int | None = None


class WorkspaceGit(BaseModel):
    """Git snapshot for the workspace card. The full porcelain listing
    stays at `/api/workspace/git`; this surface is just enough to render
    the "main · dirty" badge."""

    is_repo: bool
    branch: str | None = None
    dirty: bool = False


class WorkspaceResponse(BaseModel):
    """One workspace row plus its joined sandbox / disk / git status."""

    id: str
    display_name: str
    created_at: int
    archived_at: int | None = None
    sandbox: WorkspaceSandbox
    disk: WorkspaceDisk
    git: WorkspaceGit


class WorkspaceCreate(BaseModel):
    id: str = Field(..., min_length=2, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=200)


class WorkspaceRename(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)


class WorkspaceDiskResponse(BaseModel):
    used_mb: int | None
    quota_mb: int | None


# --- helpers ---------------------------------------------------------------


def _optional_sandbox_manager(request: Request) -> SandboxManager | None:
    """Return the sandbox manager if configured, else None.

    The CRUD list path stays usable even on a sandbox-less dev host: the
    rows still come back, just with `sandbox.state = "absent"` and disk
    + git as null/false.
    """
    return request.app.state.sandbox_manager


async def _drain_exec(
    mgr: SandboxManager,
    handle: SandboxHandle,
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
) -> tuple[int, bytes, bytes] | None:
    """Best-effort short exec collector for the aggregate list view.

    Returns (exit_code, stdout, stderr) on success. Returns None on any
    SandboxError, SandboxNotRunning, timeout, or malformed exec stream —
    the aggregate response is non-essential; one workspace's broken
    probe must not turn the whole list into a 500.
    """

    async def run() -> tuple[int, bytes, bytes] | None:
        stdout = bytearray()
        stderr = bytearray()
        exit_code: int | None = None
        try:
            async for event in mgr.exec(handle, argv, cwd=cwd):
                if isinstance(event, ExecOutput):
                    if event.stream == "stdout":
                        stdout.extend(event.data)
                    else:
                        stderr.extend(event.data)
                elif isinstance(event, ExecExit):
                    exit_code = event.exit_code
        except (SandboxError, SandboxNotRunning):
            return None
        if exit_code is None:
            return None
        return exit_code, bytes(stdout), bytes(stderr)

    if timeout is None:
        return await run()
    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except TimeoutError:
        return None


async def _probe_disk(
    mgr: SandboxManager, handle: SandboxHandle
) -> WorkspaceDisk:
    """`du -sb /workspace` → used_mb. Capped by _DISK_TIMEOUT so a wedged
    sandbox can't stall the list response. quota_mb stays None until the
    runtime exposes it cleanly (Plan-25 non-goal: surface only what
    `ResourceLimits.disk_mb` reflects on the manager, but the per-volume
    quota isn't a thing on overlayfs storage anyway)."""
    result = await _drain_exec(
        mgr,
        handle,
        ["du", "-sb", _WORKSPACE_MOUNT],
        timeout=_DISK_TIMEOUT,
    )
    if result is None:
        return WorkspaceDisk(used_mb=None, quota_mb=None)
    code, out, _ = result
    if code != 0:
        return WorkspaceDisk(used_mb=None, quota_mb=None)
    # `du -sb` prints "<bytes>\t<path>"; we only need the first token.
    first = out.decode("utf-8", "replace").strip().split(None, 1)
    if not first:
        return WorkspaceDisk(used_mb=None, quota_mb=None)
    try:
        bytes_used = int(first[0])
    except ValueError:
        return WorkspaceDisk(used_mb=None, quota_mb=None)
    used_mb = max(0, bytes_used // (1024 * 1024))
    return WorkspaceDisk(used_mb=used_mb, quota_mb=None)


async def _probe_git(
    mgr: SandboxManager, handle: SandboxHandle
) -> WorkspaceGit:
    """Lightweight branch + dirty probe. The full status listing stays
    at `/api/workspace/git` — this surface is the badge data only."""
    is_repo_result = await _drain_exec(
        mgr,
        handle,
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=_WORKSPACE_MOUNT,
    )
    if is_repo_result is None or is_repo_result[0] != 0:
        return WorkspaceGit(is_repo=False)

    branch: str | None = None
    branch_result = await _drain_exec(
        mgr,
        handle,
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=_WORKSPACE_MOUNT,
    )
    if branch_result is not None and branch_result[0] == 0:
        branch = branch_result[1].decode("utf-8", "replace").strip() or None

    dirty = False
    status_result = await _drain_exec(
        mgr,
        handle,
        ["git", "status", "--porcelain=v1"],
        cwd=_WORKSPACE_MOUNT,
    )
    if status_result is not None and status_result[0] == 0:
        dirty = bool(status_result[1].strip())

    return WorkspaceGit(is_repo=True, branch=branch, dirty=dirty)


async def _aggregate(
    workspace_id: str,
    display_name: str,
    created_at: int,
    archived_at: int | None,
    mgr: SandboxManager | None,
) -> WorkspaceResponse:
    """Build a `WorkspaceResponse` by joining the DB row with whatever the
    sandbox runtime can tell us. Cheap when no sandbox is cached: just
    reports `absent` without spinning anything up.
    """
    sandbox: WorkspaceSandbox
    disk = WorkspaceDisk()
    git = WorkspaceGit(is_repo=False)
    if mgr is None:
        sandbox = WorkspaceSandbox(state="absent")
    else:
        handle = mgr.peek_workspace(workspace_id)
        if handle is None:
            sandbox = WorkspaceSandbox(state="absent")
        else:
            try:
                live = await mgr.status(handle)
                sandbox = WorkspaceSandbox(
                    state=live.state.value,
                    exit_code=live.exit_code,
                )
            except SandboxError:
                sandbox = WorkspaceSandbox(state="absent")
            else:
                # Only probe disk/git for running containers — a crashed
                # sandbox can't answer and a `du` call would just timeout.
                if sandbox.state == "running":
                    disk = await _probe_disk(mgr, handle)
                    git = await _probe_git(mgr, handle)
    return WorkspaceResponse(
        id=workspace_id,
        display_name=display_name,
        created_at=created_at,
        archived_at=archived_at,
        sandbox=sandbox,
        disk=disk,
        git=git,
    )


# --- endpoints -------------------------------------------------------------


@router.get("", response_model=list[WorkspaceResponse])
async def api_list_workspaces(request: Request) -> list[WorkspaceResponse]:
    """List active workspaces with their sandbox + disk + git snapshot.

    Archived rows are excluded. The order is stable (display_name asc)
    so the UI sidebar's selection stays predictable across refreshes.
    """
    db: AsyncEngine = request.app.state.db
    mgr = _optional_sandbox_manager(request)
    rows = await repo.list_active(db)
    return [
        await _aggregate(
            workspace_id=row.id,
            display_name=row.display_name,
            created_at=row.created_at,
            archived_at=row.archived_at,
            mgr=mgr,
        )
        for row in rows
    ]


@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def api_create_workspace(
    request: Request, body: WorkspaceCreate
) -> WorkspaceResponse:
    """Create a new workspace row. The on-disk subdirectory is created
    lazily on first sandbox start — same pattern as today's `/api/workspace`
    surface, which doesn't pre-create either.
    """
    db: AsyncEngine = request.app.state.db
    try:
        created = await repo.create(
            db, workspace_id=body.id, display_name=body.display_name
        )
    except ValueError as exc:
        message = str(exc)
        if message == "workspace already exists":
            raise HTTPException(status_code=409, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc
    mgr = _optional_sandbox_manager(request)
    return await _aggregate(
        workspace_id=created.id,
        display_name=created.display_name,
        created_at=created.created_at,
        archived_at=created.archived_at,
        mgr=mgr,
    )


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def api_rename_workspace(
    request: Request, workspace_id: str, body: WorkspaceRename
) -> WorkspaceResponse:
    """Rename the display label. The slug never changes (it's part of the
    on-disk path). Returns 404 for unknown ids; archived rows can still
    be renamed so the UI's archive view can clean up labels."""
    db: AsyncEngine = request.app.state.db
    try:
        updated = await repo.rename(
            db, workspace_id, display_name=body.display_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    mgr = _optional_sandbox_manager(request)
    return await _aggregate(
        workspace_id=updated.id,
        display_name=updated.display_name,
        created_at=updated.created_at,
        archived_at=updated.archived_at,
        mgr=mgr,
    )


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def api_archive_workspace(
    request: Request, workspace_id: str
) -> None:
    """Soft-delete. The on-disk directory stays — hard-delete (rmtree)
    is an explicit Plan-25 non-goal. Idempotent: archiving an already-
    archived row still 204s."""
    db: AsyncEngine = request.app.state.db
    archived = await repo.archive(db, workspace_id)
    if archived is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return None


@router.get(
    "/{workspace_id}/disk",
    response_model=WorkspaceDiskResponse,
)
async def api_workspace_disk(
    request: Request, workspace_id: str
) -> WorkspaceDiskResponse:
    """One-shot disk-usage probe. Returns nulls when the sandbox is
    absent / crashed / `du` non-zero / probe times out — the panel
    renders "—" rather than misreporting a stale number.
    """
    db: AsyncEngine = request.app.state.db
    row = await repo.get(db, workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    mgr = _optional_sandbox_manager(request)
    if mgr is None:
        return WorkspaceDiskResponse(used_mb=None, quota_mb=None)
    handle = mgr.peek_workspace(workspace_id)
    if handle is None:
        return WorkspaceDiskResponse(used_mb=None, quota_mb=None)
    probe = await _probe_disk(mgr, handle)
    return WorkspaceDiskResponse(used_mb=probe.used_mb, quota_mb=probe.quota_mb)
