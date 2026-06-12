"""Mutating git endpoints: checkout, stage, unstage, discard, commit,
fetch, pull, push."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hermes.config import settings
from hermes.errors import ErrorCode

from ._internal import (
    _GIT_IDENTITY_FLAGS,
    WORKSPACE_MOUNT,
    _decode,
    _drain_exec,
    _normalise_paths,
    _require_repo,
    _resolve_git_workspace,
    _working_tree_dirty,
)
from ._models import (
    GitCheckoutRequest,
    GitCommitRequest,
    GitDiscardRequest,
    GitFetchRequest,
    GitOpResponse,
    GitPathsRequest,
    GitPullRequest,
    GitPullResponse,
    GitPushRequest,
)

router = APIRouter()


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
        raise HTTPException(
            status_code=400, detail=ErrorCode.WORKSPACE_GIT_INVALID_BRANCH.value
        )
    mgr, handle = await _resolve_git_workspace(request, body.root)
    await _require_repo(mgr, handle)

    if not body.force and await _working_tree_dirty(mgr, handle):
        raise HTTPException(
            status_code=409,
            detail=ErrorCode.WORKSPACE_GIT_DIRTY.value,
        )
    if body.force and not settings.workspace_git_destructive:
        raise HTTPException(
            status_code=403,
            detail=ErrorCode.WORKSPACE_GIT_DESTRUCTIVE_DISABLED.value,
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
            status_code=400,
            detail={
                "code": ErrorCode.WORKSPACE_GIT_COMMAND_FAILED.value,
                "params": {"command": "checkout", "stderr": _decode(err).strip()},
            },
        )
    return {"ok": True, "message": ""}


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
            status_code=400,
            detail={
                "code": ErrorCode.WORKSPACE_GIT_COMMAND_FAILED.value,
                "params": {"command": "add", "stderr": _decode(err).strip()},
            },
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
            detail={
                "code": ErrorCode.WORKSPACE_GIT_COMMAND_FAILED.value,
                "params": {
                    "command": "restore --staged",
                    "stderr": _decode(err).strip(),
                },
            },
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
            detail=ErrorCode.WORKSPACE_GIT_DESTRUCTIVE_DISABLED.value,
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
            detail={
                "code": ErrorCode.WORKSPACE_GIT_COMMAND_FAILED.value,
                "params": {
                    "command": "checkout --",
                    "stderr": _decode(err).strip(),
                },
            },
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
            detail={
                "code": ErrorCode.WORKSPACE_GIT_COMMAND_FAILED.value,
                "params": {
                    "command": "commit",
                    "stderr": _decode(err).strip(),
                },
            },
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
                detail=ErrorCode.WORKSPACE_GIT_PUSH_DETACHED_HEAD.value,
            )
        # Defence-in-depth — the current branch name comes from
        # `rev-parse` and *should* be safe, but a `-`-prefixed branch in
        # the working tree would still be parsed as a flag by git push.
        if cur.startswith("-"):
            raise HTTPException(
                status_code=400, detail=ErrorCode.WORKSPACE_GIT_INVALID_CURRENT_BRANCH.value
            )
        argv.extend(["-u", "origin", cur])

    code, _, err = await _drain_exec(mgr, handle, argv, cwd=WORKSPACE_MOUNT)
    if code != 0:
        return {"ok": False, "message": _decode(err).strip() or "git push failed"}
    return {"ok": True, "message": _decode(err).strip()}
