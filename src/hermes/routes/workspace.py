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
import hashlib
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.config import settings
from hermes.sandbox import (
    DirEntry,
    ExecExit,
    ExecOutput,
    SandboxError,
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxHandle,
    SandboxManager,
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
    # sha256 of the *on-disk bytes* (not the preview slice). Writers pass this
    # back as `base_sha` so the server can refuse stale writes with 409.
    # Null when we couldn't read the bytes (image cap exceeded / 10 MiB cap).
    sha256: str | None


class WorkspaceCreateRequest(BaseModel):
    root: str
    path: str
    content: str
    conversation_id: str = Field(min_length=1)


class WorkspaceUpdateRequest(BaseModel):
    root: str
    path: str
    content: str
    base_sha: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)


class WorkspaceRenameRequest(BaseModel):
    root: str
    src: str
    dest: str
    conversation_id: str = Field(min_length=1)


class WorkspaceDeleteRequest(BaseModel):
    root: str
    path: str
    conversation_id: str = Field(min_length=1)


class WorkspaceWriteResponse(BaseModel):
    root: str
    path: str
    sha256: str
    # Whether a git commit was produced. False when the workspace root is not
    # a git repo; the file write still happened.
    committed: bool


class WorkspaceRenameResponse(BaseModel):
    root: str
    src: str
    dest: str
    committed: bool


class GitEntry(BaseModel):
    # Porcelain v1 two-char XY status code, e.g. " M", "??", "A ", "MM".
    status: str
    path: str


class WorkspaceGitResponse(BaseModel):
    root: str
    is_repo: bool
    branch: str | None
    dirty: bool
    entries: list[GitEntry]


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


async def _drain_exec(
    mgr: SandboxManager,
    handle: SandboxHandle,
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run `argv` in the sandbox and collect (exit_code, stdout, stderr).

    Used for short, non-streaming control-plane commands (git status, git
    commit, rm, mv). Streaming exec lives elsewhere; this helper assumes the
    process produces at most a few KiB of output."""
    stdout = bytearray()
    stderr = bytearray()
    exit_code: int | None = None
    async for event in mgr.exec(handle, argv, cwd=cwd, env=env):
        if isinstance(event, ExecOutput):
            if event.stream == "stdout":
                stdout.extend(event.data)
            else:
                stderr.extend(event.data)
        elif isinstance(event, ExecExit):
            exit_code = event.exit_code
    if exit_code is None:
        # The runtime is supposed to terminate every exec stream with an
        # ExecExit; if not, treat it as a hard failure rather than silently
        # returning a fake "success".
        raise SandboxError(f"exec {argv!r} ended without exit")
    return exit_code, bytes(stdout), bytes(stderr)


# Static git identity for in-sandbox commits. The agent is the only writer
# here, so a single identity is honest about who made the change; the
# `user[conv-N]:` / `agent[conv-N]:` message prefix is what distinguishes
# the actor in `git log`.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Holzi",
    "GIT_AUTHOR_EMAIL": "holzi@local",
    "GIT_COMMITTER_NAME": "Holzi",
    "GIT_COMMITTER_EMAIL": "holzi@local",
}


async def _is_git_repo(mgr: SandboxManager, handle: SandboxHandle) -> bool:
    """True iff the workspace root is inside a git working tree.

    Each write tries to commit, so this is the gate that lets us no-op on
    repos-that-aren't. Any unexpected git failure also returns False — a
    broken `.git` shouldn't block the user's edit."""
    try:
        code, _, _ = await _drain_exec(
            mgr,
            handle,
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=WORKSPACE_MOUNT,
        )
    except SandboxError:
        return False
    return code == 0


async def _git_commit(
    mgr: SandboxManager,
    handle: SandboxHandle,
    *,
    conversation_id: str,
    action: str,
    paths: Sequence[str],
) -> bool:
    """Stage and commit `paths` with a `user[conv-N]:` message.

    Returns True if the commit was created, False if the workspace isn't a
    git repo (in which case the caller's file change has already happened
    and just isn't versioned). Other git failures are swallowed and logged
    by the runtime — a failed commit must not undo the file write."""
    if not await _is_git_repo(mgr, handle):
        return False
    summary = ", ".join(paths)
    message = f"user[conv-{conversation_id}]: {action} {summary}"
    # `git add -A` stages adds, modifies, AND deletes in one call so the same
    # helper covers all four write actions (create/update/rename/delete).
    add_argv = ["git", "add", "-A", "--", *paths]
    add_code, _, _ = await _drain_exec(
        mgr, handle, add_argv, cwd=WORKSPACE_MOUNT
    )
    if add_code != 0:
        return False
    commit_code, _, _ = await _drain_exec(
        mgr,
        handle,
        ["git", "commit", "-m", message, "--allow-empty-message"],
        cwd=WORKSPACE_MOUNT,
        env=_GIT_ENV,
    )
    return commit_code == 0


async def _stat_entry(
    mgr: SandboxManager, handle: SandboxHandle, rel: str
) -> DirEntry | None:
    """Return the DirEntry for `rel` (relative to the workspace mount) or
    None if it doesn't exist. Raises if the parent itself is missing or
    isn't a directory — those are caller-visible 400s/404s."""
    parent_rel, _, name = rel.rpartition("/")
    parent_abs = _absolute_in_sandbox(parent_rel)
    siblings = await mgr.list_dir(handle, parent_abs)
    return next((e for e in siblings if e.name == name), None)


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


# --- write endpoints (Plan 13) ----------------------------------------------


def _reject_binary_content(content: str) -> None:
    """Refuse text-write payloads that contain NUL bytes.

    The wire format is utf-8 string, so the *encoded* form is what hits the
    disk. A NUL in the encoded form is the same "this is binary, don't edit
    as text" signal `_looks_like_text` uses on reads."""
    if "\x00" in content:
        raise HTTPException(
            status_code=400,
            detail="content contains NUL bytes; binary files cannot be edited as text",
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
            status_code=404, detail="parent directory not found"
        ) from exc
    except SandboxError as exc:
        if "not a directory" in str(exc):
            raise HTTPException(
                status_code=400, detail="parent is not a directory"
            ) from exc
        raise HTTPException(
            status_code=500, detail="list_dir failed"
        ) from exc


@router.post("/file", response_model=WorkspaceWriteResponse, status_code=201)
async def api_workspace_file_create(
    request: Request, body: WorkspaceCreateRequest
) -> dict[str, Any]:
    _require_known_root(body.root)
    mgr = _require_sandbox_manager(request)
    rel = _normalise_relative(body.path)
    if rel == "":
        raise HTTPException(status_code=400, detail="path is a directory")
    _reject_binary_content(body.content)

    handle = await mgr.get_workspace(body.root)
    abs_path = _absolute_in_sandbox(rel)

    try:
        # `write_file` is documented to mkdir-p the parent, so we don't pre-check
        # the parent here — that would refuse a legitimate "create a file in a
        # fresh subdir" flow. Existence-of-target stays a 409 below. A missing
        # parent shows up as SandboxFileNotFound on the stat call; we map that
        # to "target doesn't exist" and proceed.
        try:
            existing = await _stat_entry(mgr, handle, rel)
        except SandboxFileNotFound:
            existing = None
        if existing is not None:
            # Mirror Plan 13's contract: create is the "doesn't exist yet"
            # path; the UI should show the conflict and route the user to
            # update if they want to overwrite.
            raise HTTPException(status_code=409, detail="path already exists")
        data = body.content.encode("utf-8")
        await mgr.write_file(handle, abs_path, data)
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
        ) from exc
    except SandboxFileTooLarge as exc:
        raise HTTPException(status_code=413, detail="file too large") from exc

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
    _require_known_root(body.root)
    mgr = _require_sandbox_manager(request)
    rel = _normalise_relative(body.path)
    if rel == "":
        raise HTTPException(status_code=400, detail="path is a directory")
    _reject_binary_content(body.content)

    handle = await mgr.get_workspace(body.root)
    abs_path = _absolute_in_sandbox(rel)

    try:
        existing = await _stat_entry(mgr, handle, rel)
        if existing is None:
            raise HTTPException(status_code=404, detail="path not found")
        if existing.entry_type != "file":
            raise HTTPException(
                status_code=400, detail="path is not a regular file"
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
                detail="base_sha mismatch; file changed on disk",
            )
        data = body.content.encode("utf-8")
        await mgr.write_file(handle, abs_path, data)
    except SandboxFileNotFound as exc:
        raise HTTPException(status_code=404, detail="path not found") from exc
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
        ) from exc
    except SandboxFileTooLarge as exc:
        raise HTTPException(status_code=413, detail="file too large") from exc

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
    _require_known_root(body.root)
    mgr = _require_sandbox_manager(request)
    src_rel = _normalise_relative(body.src)
    dest_rel = _normalise_relative(body.dest)
    if src_rel == "" or dest_rel == "":
        raise HTTPException(status_code=400, detail="path is a directory")
    if src_rel == dest_rel:
        raise HTTPException(
            status_code=400, detail="src and dest are the same path"
        )

    handle = await mgr.get_workspace(body.root)
    src_abs = _absolute_in_sandbox(src_rel)
    dest_abs = _absolute_in_sandbox(dest_rel)
    dest_parent_rel, _, _ = dest_rel.rpartition("/")

    try:
        src_entry = await _stat_entry(mgr, handle, src_rel)
        if src_entry is None:
            raise HTTPException(status_code=404, detail="src not found")
        if src_entry.entry_type != "file":
            raise HTTPException(
                status_code=400, detail="src is not a regular file"
            )
        await _ensure_parent_dir(mgr, handle, dest_parent_rel)
        dest_entry = await _stat_entry(mgr, handle, dest_rel)
        if dest_entry is not None:
            raise HTTPException(status_code=409, detail="dest already exists")
        # `mv` is one POSIX call so source and dest stay consistent even on
        # failure; an `exec` failure here surfaces as a 500 below.
        code, _, stderr = await _drain_exec(
            mgr, handle, ["mv", "--", src_abs, dest_abs]
        )
        if code != 0:
            raise HTTPException(
                status_code=500,
                detail=f"rename failed: {stderr.decode('utf-8', 'replace').strip()}",
            )
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
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
    _require_known_root(body.root)
    mgr = _require_sandbox_manager(request)
    rel = _normalise_relative(body.path)
    if rel == "":
        raise HTTPException(status_code=400, detail="path is a directory")

    handle = await mgr.get_workspace(body.root)
    abs_path = _absolute_in_sandbox(rel)

    try:
        entry = await _stat_entry(mgr, handle, rel)
        if entry is None:
            raise HTTPException(status_code=404, detail="path not found")
        if entry.entry_type != "file":
            # Plan 13 explicitly defers directory delete; surface a 400 so
            # the UI shows "files only" instead of silently doing nothing.
            raise HTTPException(
                status_code=400, detail="only regular files can be deleted"
            )
        code, _, stderr = await _drain_exec(
            mgr, handle, ["rm", "--", abs_path]
        )
        if code != 0:
            raise HTTPException(
                status_code=500,
                detail=f"delete failed: {stderr.decode('utf-8', 'replace').strip()}",
            )
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
        ) from exc

    committed = await _git_commit(
        mgr,
        handle,
        conversation_id=body.conversation_id,
        action="delete",
        paths=[rel],
    )
    # sha256 of an empty result; the file no longer exists. The frontend
    # treats `committed` as the authoritative signal that delete succeeded.
    return {
        "root": body.root,
        "path": rel,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "committed": committed,
    }


@router.get("/git", response_model=WorkspaceGitResponse)
async def api_workspace_git(request: Request, root: str) -> dict[str, Any]:
    _require_known_root(root)
    mgr = _require_sandbox_manager(request)
    handle = await mgr.get_workspace(root)

    try:
        if not await _is_git_repo(mgr, handle):
            return {
                "root": root,
                "is_repo": False,
                "branch": None,
                "dirty": False,
                "entries": [],
            }
        branch_code, branch_out, _ = await _drain_exec(
            mgr,
            handle,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=WORKSPACE_MOUNT,
        )
        # "HEAD" means detached — surface as-is so the UI can show the
        # detached-HEAD condition rather than pretending we're on a branch.
        branch = (
            branch_out.decode("utf-8", "replace").strip()
            if branch_code == 0
            else None
        )
        status_code, status_out, _ = await _drain_exec(
            mgr,
            handle,
            ["git", "status", "--porcelain=v1"],
            cwd=WORKSPACE_MOUNT,
        )
        if status_code != 0:
            # git failed *after* rev-parse said this is a repo — treat as
            # transient and surface an empty status; the UI shows the branch
            # without a dirty badge rather than a hard error.
            return {
                "root": root,
                "is_repo": True,
                "branch": branch,
                "dirty": False,
                "entries": [],
            }
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
        ) from exc

    entries: list[dict[str, str]] = []
    for line in status_out.decode("utf-8", "replace").splitlines():
        # porcelain v1 lines are `XY<space>path` — XY is exactly two
        # characters even when one side is blank.
        if len(line) < 4:
            continue
        status = line[:2]
        path = line[3:]
        entries.append({"status": status, "path": path})

    return {
        "root": root,
        "is_repo": True,
        "branch": branch,
        "dirty": len(entries) > 0,
        "entries": entries,
    }
