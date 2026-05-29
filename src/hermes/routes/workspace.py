"""Read-only workspace browser (Plan 12).

Roots = SandboxManager workspace ids declared in `HERMES_WORKSPACE_ROOTS`.
Tree and file reads are served by spinning up (or reusing) the matching
workspace sandbox and reading from its `/workspace` volume — the agent
container never touches the host filesystem here.

Path discipline: everything coming over the wire is a POSIX-style path
*relative* to a workspace root, with no leading `/` and no traversal
segments. The helpers below normalise the inputs and refuse anything that
would break out of `/workspace`."""

from __future__ import annotations

import base64
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes.config import settings
from hermes.sandbox import (
    DirEntry,
    SandboxError,
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxNotRunning,
)

router = APIRouter(prefix="/api/workspace")

# Workspace volume inside the sandbox container. The Podman backend mounts
# every workspace's named volume here, so all paths the API works with are
# rebased to `<WORKSPACE_MOUNT>/<relative>` before they reach the manager.
WORKSPACE_MOUNT = "/workspace"

# Preview caps layered on top of the runtime's 10 MiB read cap.
TEXT_PREVIEW_CAP = 256 * 1024
IMAGE_PREVIEW_CAP = 2 * 1024 * 1024

# How many bytes of the head we sniff to decide text-vs-binary. A NUL byte
# in this window is the standard "almost certainly binary" tell — cheaper
# than a full utf-8 decode and matches what `file(1)` does.
_BINARY_SNIFF_WINDOW = 1024

_MARKDOWN_EXTS = frozenset({".md", ".markdown"})
_IMAGE_EXTS_BY_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    # SVG intentionally absent — it's XML and previews better as text.
}


# --- response models --------------------------------------------------------


class WorkspaceRoot(BaseModel):
    id: str


class WorkspaceRootsResponse(BaseModel):
    roots: list[WorkspaceRoot]


class TreeEntry(BaseModel):
    name: str
    # `other` collapses symlinks/sockets/fifos so the UI can render them as
    # non-previewable without enumerating every POSIX type.
    type: Literal["file", "dir", "other"]
    size: int


class WorkspaceTreeResponse(BaseModel):
    root: str
    path: str
    entries: list[TreeEntry]


class WorkspaceFileResponse(BaseModel):
    root: str
    path: str
    name: str
    # Null when we couldn't determine the size cheaply (e.g. the runtime
    # refused the read because the file exceeds the hard 10 MiB cap and we
    # already returned metadata-only).
    size: int | None
    kind: Literal["text", "markdown", "image", "binary"]
    # Set only for `text`/`markdown` previews that fit within the cap.
    content: str | None
    # Set only for `image` previews that fit within the cap; data: URL with
    # the appropriate image/* MIME and base64 payload.
    data_url: str | None
    # True iff a text preview was sliced because the file exceeds
    # TEXT_PREVIEW_CAP; always False for binary/image responses.
    truncated: bool


# --- helpers ---------------------------------------------------------------


def _configured_roots() -> list[str]:
    return [r.strip() for r in settings.workspace_roots.split(",") if r.strip()]


def _require_sandbox_manager(request: Request) -> Any:
    mgr = request.app.state.sandbox_manager
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail="sandbox runtime not configured",
        )
    return mgr


def _require_known_root(root: str) -> None:
    if root not in _configured_roots():
        raise HTTPException(status_code=404, detail="unknown workspace root")


def _normalise_relative(path: str) -> str:
    """Turn an API-supplied relative path into a clean POSIX form.

    Rejects anything that would break out of `/workspace`: empty segments,
    `.`, `..`, leading slashes, and absolute paths. The empty string maps
    to the root directory."""
    if path == "":
        return ""
    # A leading slash is the most common "this is an absolute path" mistake;
    # rather than silently stripping (which would hide real bugs in callers)
    # we refuse and force the caller to fix the request.
    if path.startswith("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise HTTPException(status_code=400, detail="invalid path")
    # Defence in depth: even after segment-level checks, verify the joined
    # path normalises to a child of `/workspace`. PurePosixPath collapses
    # any residue (it won't here, but the assertion is cheap).
    joined = PurePosixPath(WORKSPACE_MOUNT) / path
    normalised = PurePosixPath(*joined.parts)
    try:
        normalised.relative_to(WORKSPACE_MOUNT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid path") from exc
    return "/".join(segments)


def _absolute_in_sandbox(rel: str) -> str:
    return WORKSPACE_MOUNT if rel == "" else f"{WORKSPACE_MOUNT}/{rel}"


def _classify_preview(name: str) -> tuple[
    Literal["text", "markdown", "image", "binary"], str | None
]:
    """Decide the upper bound of how we *might* preview a file based on its
    extension. Returns the kind plus the image MIME (when applicable).

    The actual classification is finalised after reading the bytes — a `.txt`
    that turns out to contain NULs degrades to `binary`, an `.md` that fits
    stays `markdown`, etc."""
    lower = name.lower()
    dot = lower.rfind(".")
    ext = lower[dot:] if dot != -1 else ""
    # SVG is XML and best previewed as the text source — the image cap would
    # base64-inline arbitrary XML we'd rather show editable.
    if ext in _IMAGE_EXTS_BY_MIME:
        return "image", _IMAGE_EXTS_BY_MIME[ext]
    if ext in _MARKDOWN_EXTS:
        return "markdown", None
    return "text", None


def _looks_like_text(data: bytes) -> bool:
    """Cheap text-vs-binary heuristic — used to demote a file that *looked*
    text by extension but is actually binary, and to confirm a generic file
    is safe to preview as text."""
    if b"\x00" in data[:_BINARY_SNIFF_WINDOW]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# --- endpoints --------------------------------------------------------------


@router.get("/roots", response_model=WorkspaceRootsResponse)
async def api_workspace_roots() -> dict[str, Any]:
    """List the configured workspace roots.

    Returns 200 with an empty list when nothing is configured — the frontend
    distinguishes "not configured" from "sandbox unavailable" by the absence
    of a 503 here."""
    return {"roots": [{"id": rid} for rid in _configured_roots()]}


@router.get("/tree", response_model=WorkspaceTreeResponse)
async def api_workspace_tree(
    request: Request, root: str, path: str = ""
) -> dict[str, Any]:
    _require_known_root(root)
    mgr = _require_sandbox_manager(request)
    rel = _normalise_relative(path)
    abs_path = _absolute_in_sandbox(rel)

    handle = await mgr.get_workspace(root)
    try:
        entries: list[DirEntry] = await mgr.list_dir(handle, abs_path)
    except SandboxFileNotFound as exc:
        raise HTTPException(status_code=404, detail="path not found") from exc
    except SandboxNotRunning as exc:
        # Sandbox crashed mid-request. Mirrors the chat-stream's 503 surface
        # so the frontend can offer a Restart action via the same path.
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
        ) from exc
    except SandboxError as exc:
        # "is not a directory" lands here — the API distinguishes 400 (the
        # caller pointed `/tree` at a file) from 404 (the caller pointed at
        # something that doesn't exist).
        message = str(exc)
        if "not a directory" in message:
            raise HTTPException(
                status_code=400, detail="path is not a directory"
            ) from exc
        raise HTTPException(status_code=500, detail="list_dir failed") from exc

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
    _require_known_root(root)
    mgr = _require_sandbox_manager(request)
    rel = _normalise_relative(path)
    if rel == "":
        # `/file` with an empty path would point at the workspace root —
        # callers should hit `/tree` for that, not `/file`.
        raise HTTPException(status_code=400, detail="path is a directory")
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
        raise HTTPException(status_code=404, detail="path not found") from exc
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
        ) from exc
    except SandboxError as exc:
        message = str(exc)
        if "not a directory" in message:
            # The parent itself is a file — the caller asked for a child of
            # something that can't have children.
            raise HTTPException(
                status_code=404, detail="path not found"
            ) from exc
        raise HTTPException(status_code=500, detail="list_dir failed") from exc

    target = next((e for e in siblings if e.name == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail="path not found")
    if target.entry_type == "dir":
        raise HTTPException(
            status_code=400, detail="path is a directory, use /tree"
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
            }
        try:
            data = await mgr.read_file(handle, abs_path)
        except SandboxFileNotFound as exc:
            raise HTTPException(
                status_code=404, detail="path not found"
            ) from exc
        except SandboxNotRunning as exc:
            raise HTTPException(
                status_code=503, detail="workspace sandbox not running"
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
        }
    try:
        data = await mgr.read_file(handle, abs_path)
    except SandboxFileNotFound as exc:
        raise HTTPException(status_code=404, detail="path not found") from exc
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
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
    }
