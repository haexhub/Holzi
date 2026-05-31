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
from hermes.repository import workspaces as workspaces_repo
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


# --- Plan 24: extended git surface ----------------------------------------


class GitDiffSummary(BaseModel):
    files: int
    insertions: int
    deletions: int


class GitDiffResponse(BaseModel):
    # `none` = no diff (either path is identical or there is nothing changed
    # at all). `binary` = git refuses to emit a patch; the patch field stays
    # null and the UI shows the summary only.
    kind: Literal["text", "binary", "none"]
    patch: str | None = None
    summary: GitDiffSummary
    # True when the patch body was truncated at the response cap. The summary
    # is still authoritative.
    truncated: bool = False


class GitBranch(BaseModel):
    name: str
    is_remote: bool
    last_commit_at: str | None


class GitBranchesResponse(BaseModel):
    current: str | None
    all: list[GitBranch]


class GitLogEntry(BaseModel):
    sha: str
    short_sha: str
    author: str
    subject: str
    committed_at: str


class GitCheckoutRequest(BaseModel):
    root: str
    branch: str = Field(min_length=1)
    create: bool = False
    # `force` only matters when the working tree is dirty: dirty checkout
    # without force returns 409; dirty checkout with force discards local
    # changes via `git checkout -f`, so it's gated by the destructive flag.
    force: bool = False


class GitPathsRequest(BaseModel):
    root: str
    paths: list[str] = Field(default_factory=list)


class GitDiscardRequest(BaseModel):
    root: str
    paths: list[str] = Field(default_factory=list)
    conversation_id: str = Field(min_length=1)


class GitCommitRequest(BaseModel):
    root: str
    message: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    # `all=true` stages tracked modifications before committing (git commit -a),
    # matching the "type a message and commit everything currently dirty" UX
    # path. Default false = commit whatever is already staged.
    all: bool = False


class GitFetchRequest(BaseModel):
    root: str


class GitPullRequest(BaseModel):
    root: str


class GitPushRequest(BaseModel):
    root: str
    set_upstream: bool = False


class GitOpResponse(BaseModel):
    ok: bool
    # stderr from the underlying git invocation. Surfaced verbatim so the UI
    # can show "permission denied" / "no upstream" without parsing.
    message: str = ""


class GitPullResponse(GitOpResponse):
    # Files git reported as conflicting (CONFLICT markers in stdout). Empty
    # on a clean pull; non-empty + ok=false on a conflict, returned as HTTP 200
    # so the UI doesn't have to parse a 4xx body to find the file list.
    conflicts: list[str] = Field(default_factory=list)


# --- helpers ---------------------------------------------------------------


async def _active_root_slugs(request: Request) -> list[str]:
    """Return the slugs of every non-archived workspace.

    Plan 25-A: the `workspaces` table is the source of truth. The
    `HERMES_WORKSPACE_ROOTS` env still seeds the table at boot (lifespan
    backfill in `main.py`) but is never read at request time anymore.
    """
    rows = await workspaces_repo.list_active(request.app.state.db)
    return [r.id for r in rows]


def _require_sandbox_manager(request: Request) -> Any:
    mgr = request.app.state.sandbox_manager
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail="sandbox runtime not configured",
        )
    return mgr


async def _require_known_root(request: Request, root: str) -> None:
    if root not in await _active_root_slugs(request):
        raise HTTPException(status_code=404, detail="unknown workspace root")


def _normalise_relative(path: str) -> str:
    """Turn an API-supplied relative path into a clean POSIX form.

    Rejects anything that would break out of `/workspace`: empty segments,
    `.`, `..`, leading slashes, absolute paths, AND a leading `-` (which
    would be parsed as a flag by any git/POSIX tool that doesn't use a
    `--` separator). The empty string maps to the root directory.

    Leading-`-` rejection is the *defence-in-depth* against forgetting the
    `--` separator at any call site. Plan-24's git endpoints all do pass
    `--` correctly today, but a future endpoint that bolts on `git
    something <path>` without the separator gets flag-injection for free,
    so we kill the class at the input layer."""
    if path == "":
        return ""
    # A leading slash is the most common "this is an absolute path" mistake;
    # rather than silently stripping (which would hide real bugs in callers)
    # we refuse and force the caller to fix the request.
    if path.startswith("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    if path.startswith("-"):
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


# Static git identity for in-sandbox commits, passed via `-c` flags rather
# than env vars: Podman's exec API treats `Env` as a *replacement* for the
# container env (not a merge), which would drop PATH/HOME and break the
# `git` binary lookup. The agent is the only writer here, so a single
# identity is honest about who made the change; the `user[conv-N]:` /
# `agent[conv-N]:` message prefix is what distinguishes the actor in
# `git log`.
_GIT_IDENTITY_FLAGS = (
    "-c", "user.name=Holzi",
    "-c", "user.email=holzi@local",
)


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
    commit_argv = [
        "git",
        *_GIT_IDENTITY_FLAGS,
        "commit",
        "-m",
        message,
        "--allow-empty-message",
    ]
    # Sandbox-level failures (crash mid-stream, exec ended without exit) must
    # not bubble up as a 500 after a successful file write — the docstring
    # contract is "file change happened, just not versioned".
    try:
        add_code, _, _ = await _drain_exec(
            mgr, handle, add_argv, cwd=WORKSPACE_MOUNT
        )
        if add_code != 0:
            return False
        commit_code, _, _ = await _drain_exec(
            mgr, handle, commit_argv, cwd=WORKSPACE_MOUNT
        )
    except SandboxError:
        return False
    return commit_code == 0


async def _stat_entry(
    mgr: SandboxManager, handle: SandboxHandle, rel: str
) -> DirEntry | None:
    """Return the DirEntry for `rel` (relative to the workspace mount) or
    None if `rel` (or any of its parents) doesn't exist. A parent that
    exists but isn't a directory is raised as a caller-visible 400 — that
    case is an invalid path, not just a missing target."""
    parent_rel, _, name = rel.rpartition("/")
    parent_abs = _absolute_in_sandbox(parent_rel)
    try:
        siblings = await mgr.list_dir(handle, parent_abs)
    except SandboxFileNotFound:
        # Missing parent ⇒ the target can't exist; normalise to "not found"
        # so every write caller takes the same 404 path without each having
        # to wrap _stat_entry in its own except.
        return None
    except SandboxError as exc:
        if "not a directory" in str(exc):
            raise HTTPException(
                status_code=400, detail="parent is not a directory"
            ) from exc
        raise
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
async def api_workspace_roots(request: Request) -> dict[str, Any]:
    """List the configured workspace roots.

    Returns 200 with an empty list when nothing is configured — the frontend
    distinguishes "not configured" from "sandbox unavailable" by the absence
    of a 503 here. Source of truth is the `workspaces` table (Plan 25-A);
    archived rows are excluded."""
    return {"roots": [{"id": rid} for rid in await _active_root_slugs(request)]}


@router.get("/tree", response_model=WorkspaceTreeResponse)
async def api_workspace_tree(
    request: Request, root: str, path: str = ""
) -> dict[str, Any]:
    await _require_known_root(request, root)
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
    await _require_known_root(request, root)
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
    await _require_known_root(request, body.root)
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
        # fresh subdir" flow. Existence-of-target stays a 409 below.
        existing = await _stat_entry(mgr, handle, rel)
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
    await _require_known_root(request, body.root)
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
    await _require_known_root(request, body.root)
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
    await _require_known_root(request, body.root)
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


@router.get("/git", response_model=WorkspaceGitResponse)
async def api_workspace_git(request: Request, root: str) -> dict[str, Any]:
    await _require_known_root(request, root)
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


# --- Plan 24: extended git surface ----------------------------------------

# Cap on the patch body returned by GET /git/diff. The summary is still
# computed from the *untruncated* output (via `--numstat`), so the UI shows
# accurate insertion/deletion counts even when the patch is too big to ship.
_GIT_DIFF_PATCH_CAP = 256 * 1024


async def _resolve_git_workspace(
    request: Request, root: str
) -> tuple[SandboxManager, SandboxHandle]:
    """Shared preamble: validate the root, grab the sandbox manager, spin up
    (or reuse) the workspace handle. Raises 404/503 the same way every git
    endpoint should."""
    await _require_known_root(request, root)
    mgr = _require_sandbox_manager(request)
    try:
        handle = await mgr.get_workspace(root)
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail="workspace sandbox not running"
        ) from exc
    return mgr, handle


async def _require_repo(mgr: SandboxManager, handle: SandboxHandle) -> None:
    if not await _is_git_repo(mgr, handle):
        raise HTTPException(status_code=409, detail="workspace is not a git repo")


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


async def _working_tree_dirty(
    mgr: SandboxManager, handle: SandboxHandle
) -> bool:
    """`git status --porcelain=v1` empty ⇒ clean. Any non-zero exit is
    treated as "couldn't tell" → dirty, so we err on the side of refusing a
    destructive op rather than silently nuking the user's WIP."""
    code, out, _ = await _drain_exec(
        mgr,
        handle,
        ["git", "status", "--porcelain=v1"],
        cwd=WORKSPACE_MOUNT,
    )
    if code != 0:
        return True
    return bool(_decode(out).strip())


@router.get("/git/diff", response_model=GitDiffResponse)
async def api_workspace_git_diff(
    request: Request,
    root: str,
    path: str | None = None,
    staged: bool = False,
) -> dict[str, Any]:
    """Patch + summary for the working tree (or staged changes when
    `staged=true`). When `path` is set, the diff is restricted to that file —
    relative-path discipline matches the rest of the workspace API."""
    mgr, handle = await _resolve_git_workspace(request, root)
    await _require_repo(mgr, handle)

    argv = ["git", "diff", "--no-color", "--src-prefix=a/", "--dst-prefix=b/"]
    if staged:
        argv.append("--staged")
    if path is not None:
        rel = _normalise_relative(path)
        if rel == "":
            raise HTTPException(status_code=400, detail="path must be a file")
        argv.extend(["--", rel])

    patch_code, patch_out, patch_err = await _drain_exec(
        mgr, handle, argv, cwd=WORKSPACE_MOUNT
    )
    if patch_code != 0:
        # Surface the stderr so the user can see e.g. "ambiguous argument".
        raise HTTPException(
            status_code=400, detail=_decode(patch_err).strip() or "git diff failed"
        )
    patch_bytes = bytes(patch_out)

    # `--numstat` gives `additions<TAB>deletions<TAB>path` (or `-\t-\tpath`
    # for binary). One row per changed file, so the row count *is* the
    # files-changed total — no need for a second `--shortstat` pass.
    numstat_argv = argv[:1] + ["diff", "--numstat"]
    if staged:
        numstat_argv.append("--staged")
    if path is not None:
        numstat_argv.extend(["--", _normalise_relative(path)])
    num_code, num_out, _ = await _drain_exec(
        mgr, handle, numstat_argv, cwd=WORKSPACE_MOUNT
    )
    files = 0
    insertions = 0
    deletions = 0
    is_binary = False
    if num_code == 0:
        for line in _decode(num_out).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            if parts[0] == "-" and parts[1] == "-":
                is_binary = True
                continue
            try:
                insertions += int(parts[0])
                deletions += int(parts[1])
            except ValueError:
                continue

    if files == 0 and not patch_bytes:
        return {
            "kind": "none",
            "patch": None,
            "summary": {"files": 0, "insertions": 0, "deletions": 0},
            "truncated": False,
        }

    if is_binary and not patch_bytes:
        return {
            "kind": "binary",
            "patch": None,
            "summary": {
                "files": files,
                "insertions": insertions,
                "deletions": deletions,
            },
            "truncated": False,
        }

    truncated = len(patch_bytes) > _GIT_DIFF_PATCH_CAP
    if truncated:
        patch_bytes = patch_bytes[:_GIT_DIFF_PATCH_CAP]
    return {
        "kind": "text",
        "patch": _decode(patch_bytes),
        "summary": {
            "files": files,
            "insertions": insertions,
            "deletions": deletions,
        },
        "truncated": truncated,
    }


@router.get("/git/branches", response_model=GitBranchesResponse)
async def api_workspace_git_branches(
    request: Request, root: str
) -> dict[str, Any]:
    mgr, handle = await _resolve_git_workspace(request, root)
    await _require_repo(mgr, handle)

    cur_code, cur_out, _ = await _drain_exec(
        mgr,
        handle,
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=WORKSPACE_MOUNT,
    )
    current = _decode(cur_out).strip() if cur_code == 0 else None
    if current == "HEAD":
        current = None  # detached

    # `for-each-ref` with a custom format gives us every local *and* remote
    # ref in one pass, with the committer date in ISO-8601 (so the UI can
    # render "last touched X ago" without re-parsing).
    fmt = "%(refname:short)|%(committerdate:iso-strict)|%(refname)"
    code, out, _ = await _drain_exec(
        mgr,
        handle,
        ["git", "for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes"],
        cwd=WORKSPACE_MOUNT,
    )
    branches: list[dict[str, Any]] = []
    if code == 0:
        for line in _decode(out).splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            short = parts[0]
            iso = parts[1] or None
            full = parts[2]
            is_remote = full.startswith("refs/remotes/")
            # `origin/HEAD` is a symbolic ref and confuses the UI — skip it.
            if is_remote and short.endswith("/HEAD"):
                continue
            branches.append(
                {"name": short, "is_remote": is_remote, "last_commit_at": iso}
            )

    return {"current": current, "all": branches}


@router.get("/git/log", response_model=list[GitLogEntry])
async def api_workspace_git_log(
    request: Request,
    root: str,
    path: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1..200")
    mgr, handle = await _resolve_git_workspace(request, root)
    await _require_repo(mgr, handle)

    # `%x1f` (ASCII unit separator) is more robust than `|` — commit subjects
    # routinely contain pipes, but never a 0x1f byte.
    fmt = "%H%x1f%h%x1f%an%x1f%cI%x1f%s"
    argv = [
        "git",
        "log",
        f"--pretty=format:{fmt}",
        f"-n{limit}",
    ]
    if path is not None:
        rel = _normalise_relative(path)
        argv.extend(["--", rel])

    code, out, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        raise HTTPException(
            status_code=400, detail=_decode(err).strip() or "git log failed"
        )
    entries: list[dict[str, Any]] = []
    for line in _decode(out).splitlines():
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        sha, short, author, committed_at, subject = parts[:5]
        entries.append(
            {
                "sha": sha,
                "short_sha": short,
                "author": author,
                "subject": subject,
                "committed_at": committed_at,
            }
        )
    return entries


@router.post("/git/checkout", response_model=GitOpResponse)
async def api_workspace_git_checkout(
    request: Request, body: GitCheckoutRequest
) -> dict[str, Any]:
    # A leading `-` would be parsed by `git checkout` as a flag, not a ref —
    # `git checkout --orphan x` is a destructive operation that bypasses
    # the dirty-tree gate, and `git checkout -B existing` overwrites a
    # branch ref. Reject at the input layer; `git checkout -- branch`
    # doesn't work as a workaround because the `--` would have to come
    # after the new branch name in the `-b` form.
    if body.branch.startswith("-"):
        raise HTTPException(status_code=400, detail="invalid branch name")
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)

    if not body.force and await _working_tree_dirty(mgr, handle):
        raise HTTPException(
            status_code=409,
            detail="working tree has uncommitted changes",
        )
    if body.force and not settings.workspace_git_destructive:
        raise HTTPException(
            status_code=403,
            detail="destructive git ops disabled (set HERMES_WORKSPACE_GIT_DESTRUCTIVE=1)",
        )

    argv: list[str] = ["git", "checkout"]
    if body.force:
        argv.append("-f")
    if body.create:
        argv.append("-b")
    argv.append(body.branch)

    code, _, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        raise HTTPException(
            status_code=400, detail=_decode(err).strip() or "git checkout failed"
        )
    return {"ok": True, "message": ""}


def _normalise_paths(paths: Sequence[str]) -> list[str]:
    """Empty list = whole tree → callers translate to `-A`. Anything else
    must be safe relative paths (same discipline as the file endpoints)."""
    return [_normalise_relative(p) for p in paths]


@router.post("/git/stage", response_model=GitOpResponse)
async def api_workspace_git_stage(
    request: Request, body: GitPathsRequest
) -> dict[str, Any]:
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)
    rels = _normalise_paths(body.paths)

    argv = ["git", "add"]
    if not rels:
        argv.append("-A")
    else:
        argv.extend(["--", *rels])
    code, _, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        raise HTTPException(
            status_code=400, detail=_decode(err).strip() or "git add failed"
        )
    return {"ok": True, "message": ""}


@router.post("/git/unstage", response_model=GitOpResponse)
async def api_workspace_git_unstage(
    request: Request, body: GitPathsRequest
) -> dict[str, Any]:
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)
    rels = _normalise_paths(body.paths)

    # `git restore --staged` is the modern path; targets either a list of
    # paths or `.` for "everything currently staged". Returns 0 on success
    # even when nothing was actually changed.
    argv = ["git", "restore", "--staged"]
    if not rels:
        argv.append(".")
    else:
        argv.extend(["--", *rels])
    code, _, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        raise HTTPException(
            status_code=400,
            detail=_decode(err).strip() or "git restore --staged failed",
        )
    return {"ok": True, "message": ""}


@router.post("/git/discard", response_model=GitOpResponse)
async def api_workspace_git_discard(
    request: Request, body: GitDiscardRequest
) -> dict[str, Any]:
    """Destructive: throws away unstaged changes in `paths`. Gated by the
    `HERMES_WORKSPACE_GIT_DESTRUCTIVE` env flag — without it the endpoint
    refuses (403) so a misconfigured deployment can't lose work."""
    if not settings.workspace_git_destructive:
        raise HTTPException(
            status_code=403,
            detail="destructive git ops disabled (set HERMES_WORKSPACE_GIT_DESTRUCTIVE=1)",
        )
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)
    rels = _normalise_paths(body.paths)

    argv = ["git", "checkout", "--"]
    if not rels:
        argv = ["git", "checkout", "--", "."]
    else:
        argv.extend(rels)
    code, _, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        raise HTTPException(
            status_code=400,
            detail=_decode(err).strip() or "git checkout -- failed",
        )
    return {"ok": True, "message": ""}


@router.post("/git/commit", response_model=GitOpResponse)
async def api_workspace_git_commit(
    request: Request, body: GitCommitRequest
) -> dict[str, Any]:
    """Explicit user-message commit. The Plan-13 auto-commit (`user[conv-N]:
    …`) still fires on every file write; this is the path the Git tab uses
    when the user types their own message."""
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)

    # `[user]` prefix keeps the Git-tab UI's commits visually distinct from
    # the auto-commit `user[conv-N]:` line. The conversation id still ends up
    # in the message so `git log` carries the same audit trail as the
    # auto-commits.
    message = f"[user conv-{body.conversation_id}] {body.message}"
    argv: list[str] = [
        "git",
        *_GIT_IDENTITY_FLAGS,
        "commit",
        "-m",
        message,
    ]
    if body.all:
        argv.insert(argv.index("commit") + 1, "-a")
    code, _, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        raise HTTPException(
            status_code=400,
            detail=_decode(err).strip() or "git commit failed",
        )
    return {"ok": True, "message": ""}


@router.post("/git/fetch", response_model=GitOpResponse)
async def api_workspace_git_fetch(
    request: Request, body: GitFetchRequest
) -> dict[str, Any]:
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)

    code, _, err = await _drain_exec(
        mgr, handle, ["git", "fetch", "--prune"], cwd=WORKSPACE_MOUNT
    )
    if code != 0:
        return {"ok": False, "message": _decode(err).strip() or "git fetch failed"}
    return {"ok": True, "message": _decode(err).strip()}


@router.post("/git/pull", response_model=GitPullResponse)
async def api_workspace_git_pull(
    request: Request, body: GitPullRequest
) -> dict[str, Any]:
    """Pulls the upstream; conflicts surface as `ok=false` + a file list,
    *not* an HTTP error — the UI shows the list inline and tells the user
    to resolve in their editor."""
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)

    # `--no-rebase` is the deliberate default: some users have
    # `pull.rebase=true` in their gitconfig and would be surprised when the
    # workspace pulls differently from their CLI. The UI doesn't yet expose
    # a "rebase / merge" toggle, so we pin the strategy here.
    code, out, err = await _drain_exec(
        mgr,
        handle,
        ["git", *_GIT_IDENTITY_FLAGS, "pull", "--no-rebase"],
        cwd=WORKSPACE_MOUNT,
    )
    stdout = _decode(out)
    stderr = _decode(err)
    if code == 0:
        return {"ok": True, "message": stderr.strip(), "conflicts": []}

    # Conflict surfacing: ask git itself for the unmerged paths.
    # `--name-only --diff-filter=U` enumerates exactly the files with
    # merge conflicts regardless of the CONFLICT variant (content,
    # modify/delete, rename/rename, add/add, …). A line-grep over the
    # `CONFLICT (...): ... in <path>` strings looks tempting but most
    # variants put the path in a different position or include a branch
    # name after ` in `, so parsing the raw output gives the wrong path
    # for everything except the canonical "content" conflict.
    _, names_out, _ = await _drain_exec(
        mgr,
        handle,
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=WORKSPACE_MOUNT,
    )
    conflicts: list[str] = [
        line.strip()
        for line in _decode(names_out).splitlines()
        if line.strip()
    ]
    message = stderr.strip() or stdout.strip() or "git pull failed"
    return {"ok": False, "message": message, "conflicts": conflicts}


@router.post("/git/push", response_model=GitOpResponse)
async def api_workspace_git_push(
    request: Request, body: GitPushRequest
) -> dict[str, Any]:
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)

    argv: list[str] = ["git", "push"]
    if body.set_upstream:
        # `-u` requires a branch name; resolve the current branch so the
        # client doesn't have to pass it explicitly.
        cur_code, cur_out, _ = await _drain_exec(
            mgr,
            handle,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=WORKSPACE_MOUNT,
        )
        cur = _decode(cur_out).strip() if cur_code == 0 else ""
        if not cur or cur == "HEAD":
            raise HTTPException(
                status_code=400,
                detail="cannot push --set-upstream from a detached HEAD",
            )
        # Defence-in-depth — the current branch name comes from
        # `rev-parse` and *should* be safe, but a `-`-prefixed branch in
        # the working tree would still be parsed as a flag by git push.
        if cur.startswith("-"):
            raise HTTPException(
                status_code=400, detail="invalid current branch name"
            )
        argv.extend(["-u", "origin", cur])

    code, _, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        return {"ok": False, "message": _decode(err).strip() or "git push failed"}
    return {"ok": True, "message": _decode(err).strip()}
