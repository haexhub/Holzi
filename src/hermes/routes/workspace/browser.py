"""Read-only workspace browser (Plan 12): `/roots`, `/tree`, `/file` (GET)."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.errors import ErrorCode
from hermes.routes._helpers import require_sandbox_manager
from hermes.sandbox import (
    DirEntry,
    SandboxError,
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxNotRunning,
)

from ._internal import (
    IMAGE_PREVIEW_CAP,
    TEXT_PREVIEW_CAP,
    _absolute_in_sandbox,
    _active_root_slugs,
    _classify_preview,
    _looks_like_text,
    _normalise_relative,
    _require_known_root,
)
from ._models import (
    WorkspaceFileResponse,
    WorkspaceRootsResponse,
    WorkspaceTreeResponse,
)

router = APIRouter()


@router.get("/roots", response_model=WorkspaceRootsResponse)
async def api_workspace_roots(request: Request) -> dict[str, Any]:
    """List the active workspace roots.

    Returns 200 with an empty list when nothing is configured — the frontend
    distinguishes "not configured" from "sandbox unavailable" by the absence
    of a 503 here. The slug list is sourced from `workspaces.list_active`
    (Plan 25-A); the legacy `HERMES_WORKSPACE_ROOTS` env is bootstrap-only."""
    db: AsyncEngine = request.app.state.db
    return {"roots": [{"id": rid} for rid in await _active_root_slugs(db)]}


@router.get("/tree", response_model=WorkspaceTreeResponse)
async def api_workspace_tree(
    request: Request, root: str, path: str = ""
) -> dict[str, Any]:
    await _require_known_root(request, root)
    mgr = require_sandbox_manager(request)
    rel = _normalise_relative(path)
    abs_path = _absolute_in_sandbox(rel)

    handle = await mgr.get_workspace(root)
    try:
        entries: list[DirEntry] = await mgr.list_dir(handle, abs_path)
    except SandboxFileNotFound as exc:
        raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
        ) from exc
    except SandboxNotRunning as exc:
        # Sandbox crashed mid-request. Mirrors the chat-stream's 503 surface
        # so the frontend can offer a Restart action via the same path.
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc
    except SandboxError as exc:
        # "is not a directory" lands here — the API distinguishes 400 (the
        # caller pointed `/tree` at a file) from 404 (the caller pointed at
        # something that doesn't exist).
        message = str(exc)
        if "not a directory" in message:
            raise HTTPException(
                status_code=400, detail=ErrorCode.WORKSPACE_PATH_NOT_DIRECTORY.value
            ) from exc
        raise HTTPException(
            status_code=500, detail=ErrorCode.WORKSPACE_LIST_FAILED.value
        ) from exc

    return {
        "root": root,
        "path": rel,
        "entries": [
            {"name": e.name, "type": e.entry_type, "size": e.size}
            for e in entries
        ],
    }


@router.get("/file", response_model=WorkspaceFileResponse)
async def api_workspace_file(
    request: Request, root: str, path: str
) -> dict[str, Any]:
    await _require_known_root(request, root)
    mgr = require_sandbox_manager(request)
    rel = _normalise_relative(path)
    if rel == "":
        # `/file` with an empty path would point at the workspace root —
        # callers should hit `/tree` for that, not `/file`.
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_PATH_IS_DIRECTORY.value
        )
    abs_path = _absolute_in_sandbox(rel)
    parent_rel, _, name = rel.rpartition("/")
    parent_abs = _absolute_in_sandbox(parent_rel)

    handle = await mgr.get_workspace(root)

    # Stat via list_dir on the parent so we know the size + type before we
    # decide whether to read the body. Cheaper than calling read_file then
    # discovering the file is 9 MiB.
    try:
        siblings: list[DirEntry] = await mgr.list_dir(handle, parent_abs)
    except SandboxFileNotFound as exc:
        raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
        ) from exc
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc
    except SandboxError as exc:
        message = str(exc)
        if "not a directory" in message:
            # The parent itself is a file — the caller asked for a child of
            # something that can't have children.
            raise HTTPException(
                status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
            ) from exc
        raise HTTPException(
            status_code=500, detail=ErrorCode.WORKSPACE_LIST_FAILED.value
        ) from exc

    target = next((e for e in siblings if e.name == name), None)
    if target is None:
        raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
        )
    if target.entry_type == "dir":
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_PATH_IS_DIRECTORY_USE_TREE.value
        )
    if target.entry_type == "other":
        # Symlinks/sockets/etc. — surface as a binary placeholder so the UI
        # can show "not previewable" without claiming the file doesn't exist.
        return {
            "root": root,
            "path": rel,
            "name": name,
            "size": target.size,
            "kind": "binary",
            "content": None,
            "data_url": None,
            "truncated": False,
            "sha256": None,
        }

    kind_hint, image_mime = _classify_preview(name)
    size = target.size

    # Image (incl. SVG) path: don't even attempt to read past the image cap.
    if kind_hint == "image":
        assert image_mime is not None
        if size > IMAGE_PREVIEW_CAP:
            return {
                "root": root,
                "path": rel,
                "name": name,
                "size": size,
                "kind": "image",
                "content": None,
                "data_url": None,
                "truncated": False,
                "sha256": None,
            }
        try:
            data = await mgr.read_file(handle, abs_path)
        except SandboxFileNotFound as exc:
            raise HTTPException(
                status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
            ) from exc
        except SandboxNotRunning as exc:
            raise HTTPException(
                status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
            ) from exc
        except SandboxFileTooLarge:
            return {
                "root": root,
                "path": rel,
                "name": name,
                "size": None,
                "kind": "binary",
                "content": None,
                "data_url": None,
                "truncated": False,
                "sha256": None,
            }
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "root": root,
            "path": rel,
            "name": name,
            "size": size,
            "kind": "image",
            "content": None,
            "data_url": f"data:{image_mime};base64,{encoded}",
            "truncated": False,
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    # Text / markdown hint path: read up to the preview cap + 1 to detect
    # truncation, then validate it's actually text. A `.md` with NULs falls
    # back to binary; a plain extensionless file that *is* text stays text.
    requested = TEXT_PREVIEW_CAP + 1 if size > TEXT_PREVIEW_CAP else size
    if requested == 0:
        return {
            "root": root,
            "path": rel,
            "name": name,
            "size": 0,
            "kind": "markdown" if kind_hint == "markdown" else "text",
            "content": "",
            "data_url": None,
            "truncated": False,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    try:
        data = await mgr.read_file(handle, abs_path)
    except SandboxFileNotFound as exc:
        raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_PATH_NOT_FOUND.value
        ) from exc
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc
    except SandboxFileTooLarge:
        # The runtime's hard 10 MiB cap kicked in before we could even
        # decide whether the file was text-ish. Report metadata-only.
        return {
            "root": root,
            "path": rel,
            "name": name,
            "size": None,
            "kind": "binary",
            "content": None,
            "data_url": None,
            "truncated": False,
            "sha256": None,
        }

    truncated = len(data) > TEXT_PREVIEW_CAP
    preview_bytes = data[:TEXT_PREVIEW_CAP]
    if not _looks_like_text(preview_bytes):
        return {
            "root": root,
            "path": rel,
            "name": name,
            "size": size,
            "kind": "binary",
            "content": None,
            "data_url": None,
            "truncated": False,
            "sha256": None,
        }
    # Decode the (possibly sliced) preview. utf-8 boundary can fall mid
    # codepoint after the slice — `errors="replace"` keeps the response
    # valid utf-8 without flagging an otherwise-fine text file as binary.
    content = preview_bytes.decode("utf-8", errors="replace")
    return {
        "root": root,
        "path": rel,
        "name": name,
        "size": size,
        "kind": "markdown" if kind_hint == "markdown" else "text",
        "content": content,
        "data_url": None,
        "truncated": truncated,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
