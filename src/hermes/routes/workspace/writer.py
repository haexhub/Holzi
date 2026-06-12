"""File-mutating endpoints (Plan 13): POST/PUT/DELETE `/file` and `/rename`."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hermes.errors import ErrorCode
from hermes.routes._helpers import require_sandbox_manager
from hermes.sandbox import (
    SandboxError,
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxHandle,
    SandboxManager,
    SandboxNotRunning,
)

from ._internal import (
    _absolute_in_sandbox,
    _drain_exec,
    _git_commit,
    _normalise_relative,
    _require_known_root,
    _stat_entry,
)
from ._models import (
    WorkspaceCreateRequest,
    WorkspaceDeleteRequest,
    WorkspaceRenameRequest,
    WorkspaceRenameResponse,
    WorkspaceUpdateRequest,
    WorkspaceWriteResponse,
)

router = APIRouter()


# --- write endpoints (Plan 13) ----------------------------------------------


def _reject_binary_content(content: str) -> None:
    """Refuse text-write payloads that contain NUL bytes.

    The wire format is utf-8 string, so the *encoded* form is what hits the
    disk. A NUL in the encoded form is the same "this is binary, don't edit
    as text" signal `_looks_like_text` uses on reads."""
    if "\x00" in content:
        raise HTTPException(
            status_code=400,
            detail=ErrorCode.WORKSPACE_FILE_NUL_BYTES.value,
        )


async def _ensure_parent_dir(
    mgr: SandboxManager, handle: SandboxHandle, parent_rel: str
) -> None:
    """Ensure the parent directory of a new file exists. Treat a missing
    parent as 404 — the panel's create flow targets an existing directory,
    so a missing parent is a stale UI, not an mkdir-p situation."""
    if parent_rel == "":
        return
    parent_abs = _absolute_in_sandbox(parent_rel)
    try:
        await mgr.list_dir(handle, parent_abs)
    except SandboxFileNotFound as exc:
        raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PARENT_NOT_FOUND.value
        ) from exc
    except SandboxError as exc:
        if "not a directory" in str(exc):
            raise HTTPException(
                status_code=400, detail=ErrorCode.WORKSPACE_PARENT_NOT_DIRECTORY.value
            ) from exc
        raise HTTPException(
            status_code=500, detail=ErrorCode.WORKSPACE_LIST_FAILED.value
        ) from exc


@router.post("/file", response_model=WorkspaceWriteResponse, status_code=201)
async def api_workspace_file_create(
    request: Request, body: WorkspaceCreateRequest
) -> dict[str, Any]:
    await _require_known_root(request, body.root)
    mgr = require_sandbox_manager(request)
    rel = _normalise_relative(body.path)
    if rel == "":
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_PATH_IS_DIRECTORY.value
        )
    _reject_binary_content(body.content)

    handle = await mgr.get_workspace(body.root)
    abs_path = _absolute_in_sandbox(rel)

    try:
        # `write_file` is documented to mkdir-p the parent, so we don't pre-check
        # the parent here — that would refuse a legitimate "create a file in a
        # fresh subdir" flow. Existence-of-target stays a 409 below.
        existing = await _stat_entry(mgr, handle, rel)
        if existing is not None:
            # Mirror Plan 13's contract: create is the "doesn't exist yet"
            # path; the UI should show the conflict and route the user to
            # update if they want to overwrite.
            raise HTTPException(
                status_code=409, detail=ErrorCode.WORKSPACE_PATH_EXISTS.value
            )
        data = body.content.encode("utf-8")
        await mgr.write_file(handle, abs_path, data)
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc
    except SandboxFileTooLarge as exc:
        raise HTTPException(
            status_code=413, detail=ErrorCode.WORKSPACE_FILE_TOO_LARGE.value
        ) from exc

    committed = await _git_commit(
        mgr,
        handle,
        conversation_id=body.conversation_id,
        action="create",
        paths=[rel],
    )
    return {
        "root": body.root,
        "path": rel,
        "sha256": hashlib.sha256(data).hexdigest(),
        "committed": committed,
    }


@router.put("/file", response_model=WorkspaceWriteResponse)
async def api_workspace_file_update(
    request: Request, body: WorkspaceUpdateRequest
) -> dict[str, Any]:
    await _require_known_root(request, body.root)
    mgr = require_sandbox_manager(request)
    rel = _normalise_relative(body.path)
    if rel == "":
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_PATH_IS_DIRECTORY.value
        )
    _reject_binary_content(body.content)

    handle = await mgr.get_workspace(body.root)
    abs_path = _absolute_in_sandbox(rel)

    try:
        existing = await _stat_entry(mgr, handle, rel)
        if existing is None:
            raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
        )
        if existing.entry_type != "file":
            raise HTTPException(
                status_code=400, detail=ErrorCode.WORKSPACE_PATH_NOT_REGULAR.value
            )
        # Re-read the on-disk bytes to compute the *current* sha. The
        # base_sha check is the only thing protecting against a concurrent
        # writer (the agent itself, in bind-mount mode also a host editor)
        # silently overwriting the user's changes.
        current = await mgr.read_file(handle, abs_path)
        current_sha = hashlib.sha256(current).hexdigest()
        if current_sha != body.base_sha:
            raise HTTPException(
                status_code=409,
                detail=ErrorCode.WORKSPACE_FILE_SHA_MISMATCH.value,
            )
        data = body.content.encode("utf-8")
        await mgr.write_file(handle, abs_path, data)
    except SandboxFileNotFound as exc:
        raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
        ) from exc
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc
    except SandboxFileTooLarge as exc:
        raise HTTPException(
            status_code=413, detail=ErrorCode.WORKSPACE_FILE_TOO_LARGE.value
        ) from exc

    committed = await _git_commit(
        mgr,
        handle,
        conversation_id=body.conversation_id,
        action="edit",
        paths=[rel],
    )
    return {
        "root": body.root,
        "path": rel,
        "sha256": hashlib.sha256(data).hexdigest(),
        "committed": committed,
    }


@router.post("/rename", response_model=WorkspaceRenameResponse)
async def api_workspace_file_rename(
    request: Request, body: WorkspaceRenameRequest
) -> dict[str, Any]:
    await _require_known_root(request, body.root)
    mgr = require_sandbox_manager(request)
    src_rel = _normalise_relative(body.src)
    dest_rel = _normalise_relative(body.dest)
    if src_rel == "" or dest_rel == "":
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_PATH_IS_DIRECTORY.value
        )
    if src_rel == dest_rel:
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_RENAME_SAME_PATH.value
        )

    handle = await mgr.get_workspace(body.root)
    src_abs = _absolute_in_sandbox(src_rel)
    dest_abs = _absolute_in_sandbox(dest_rel)
    dest_parent_rel, _, _ = dest_rel.rpartition("/")

    try:
        src_entry = await _stat_entry(mgr, handle, src_rel)
        if src_entry is None:
            raise HTTPException(
                status_code=404, detail=ErrorCode.WORKSPACE_RENAME_SRC_NOT_FOUND.value
            )
        if src_entry.entry_type != "file":
            raise HTTPException(
                status_code=400, detail=ErrorCode.WORKSPACE_RENAME_SRC_NOT_REGULAR.value
            )
        await _ensure_parent_dir(mgr, handle, dest_parent_rel)
        dest_entry = await _stat_entry(mgr, handle, dest_rel)
        if dest_entry is not None:
            raise HTTPException(
                status_code=409, detail=ErrorCode.WORKSPACE_RENAME_DEST_EXISTS.value
            )
        # `mv` is one POSIX call so source and dest stay consistent even on
        # failure; an `exec` failure here surfaces as a 500 below.
        code, _, stderr = await _drain_exec(
            mgr, handle, ["mv", "--", src_abs, dest_abs]
        )
        if code != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": ErrorCode.WORKSPACE_RENAME_FAILED.value,
                    "params": {
                        "stderr": stderr.decode("utf-8", "replace").strip(),
                    },
                },
            )
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc

    committed = await _git_commit(
        mgr,
        handle,
        conversation_id=body.conversation_id,
        action="rename",
        paths=[src_rel, dest_rel],
    )
    return {
        "root": body.root,
        "src": src_rel,
        "dest": dest_rel,
        "committed": committed,
    }


# FastAPI's `delete` decorator does not accept a request body via the
# `body=...` convention, but plain JSON in the body works fine — Pydantic
# parses it the same way. The conversation_id rides in the body to keep
# the wire shape uniform with create/update/rename.
@router.api_route(
    "/file",
    methods=["DELETE"],
    response_model=WorkspaceWriteResponse,
)
async def api_workspace_file_delete(
    request: Request, body: WorkspaceDeleteRequest
) -> dict[str, Any]:
    await _require_known_root(request, body.root)
    mgr = require_sandbox_manager(request)
    rel = _normalise_relative(body.path)
    if rel == "":
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_PATH_IS_DIRECTORY.value
        )

    handle = await mgr.get_workspace(body.root)
    abs_path = _absolute_in_sandbox(rel)

    try:
        entry = await _stat_entry(mgr, handle, rel)
        if entry is None:
            raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
        )
        if entry.entry_type != "file":
            # Plan 13 explicitly defers directory delete; surface a 400 so
            # the UI shows "files only" instead of silently doing nothing.
            raise HTTPException(
                status_code=400, detail=ErrorCode.WORKSPACE_DELETE_NOT_REGULAR.value
            )
        code, _, stderr = await _drain_exec(
            mgr, handle, ["rm", "--", abs_path]
        )
        if code != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": ErrorCode.WORKSPACE_DELETE_FAILED.value,
                    "params": {
                        "stderr": stderr.decode("utf-8", "replace").strip(),
                    },
                },
            )
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc

    committed = await _git_commit(
        mgr,
        handle,
        conversation_id=body.conversation_id,
        action="delete",
        paths=[rel],
    )
    # sha256 of an empty result; the file no longer exists. `committed`
    # reflects only whether a git commit was created — for non-git
    # workspaces (or commit failures) the delete itself still succeeded
    # (HTTP 200), the change is just not versioned.
    return {
        "root": body.root,
        "path": rel,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "committed": committed,
    }
