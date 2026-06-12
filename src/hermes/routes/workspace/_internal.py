"""Private helpers shared across the workspace sub-routers.

Everything here is package-private (leading underscore). Callers outside
the `workspace` package have no business reaching in — the public surface
is the HTTP router."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Literal

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.errors import ErrorCode
from hermes.repository import workspaces as workspaces_repo
from hermes.routes._helpers import require_sandbox_manager
from hermes.sandbox import (
    DirEntry,
    ExecExit,
    ExecOutput,
    SandboxError,
    SandboxFileNotFound,
    SandboxHandle,
    SandboxManager,
    SandboxNotRunning,
)

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


# --- helpers ---------------------------------------------------------------


async def _active_root_slugs(db: AsyncEngine) -> list[str]:
    """Plan 25-A: the `workspaces` table is the source of truth. The env
    `HERMES_WORKSPACE_ROOTS` stays as the boot-time backfill mechanism
    (see `main.py` lifespan), but every request-time root check reads the
    live DB so a workspace created via `POST /api/workspaces` is visible
    to the browser + git endpoints without a container restart.
    """
    rows = await workspaces_repo.list_active(db)
    return [r.id for r in rows]


async def _require_known_root(request: Request, root: str) -> None:
    db: AsyncEngine = request.app.state.db
    if root not in await _active_root_slugs(db):
        raise HTTPException(
            status_code=404, detail=ErrorCode.WORKSPACE_ROOT_NOT_FOUND.value
        )


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
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_INVALID_PATH.value
        )
    if path.startswith("-"):
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_INVALID_PATH.value
        )
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_INVALID_PATH.value
        )
    # Defence in depth: even after segment-level checks, verify the joined
    # path normalises to a child of `/workspace`. PurePosixPath collapses
    # any residue (it won't here, but the assertion is cheap).
    joined = PurePosixPath(WORKSPACE_MOUNT) / path
    normalised = PurePosixPath(*joined.parts)
    try:
        normalised.relative_to(WORKSPACE_MOUNT)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_INVALID_PATH.value
        ) from exc
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
                status_code=400, detail=ErrorCode.WORKSPACE_PARENT_NOT_DIRECTORY.value
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


async def _resolve_git_workspace(
    request: Request, root: str
) -> tuple[SandboxManager, SandboxHandle]:
    """Shared preamble: validate the root, grab the sandbox manager, spin up
    (or reuse) the workspace handle. Raises 404/503 the same way every git
    endpoint should."""
    await _require_known_root(request, root)
    mgr = require_sandbox_manager(request)
    try:
        handle = await mgr.get_workspace(root)
    except SandboxNotRunning as exc:
        raise HTTPException(
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
        ) from exc
    return mgr, handle


async def _require_repo(mgr: SandboxManager, handle: SandboxHandle) -> None:
    if not await _is_git_repo(mgr, handle):
        raise HTTPException(
            status_code=409, detail=ErrorCode.WORKSPACE_NOT_GIT_REPO.value
        )


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


def _normalise_paths(paths: Sequence[str]) -> list[str]:
    """Empty list = whole tree → callers translate to `-A`. Anything else
    must be safe relative paths (same discipline as the file endpoints)."""
    return [_normalise_relative(p) for p in paths]
