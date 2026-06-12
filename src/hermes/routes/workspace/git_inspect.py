"""Read-only git endpoints: status (`/git`), diff, branches, log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hermes.errors import ErrorCode
from hermes.routes._helpers import require_sandbox_manager
from hermes.sandbox import SandboxNotRunning

from ._internal import (
    WORKSPACE_MOUNT,
    _decode,
    _drain_exec,
    _is_git_repo,
    _normalise_relative,
    _require_known_root,
    _require_repo,
    _resolve_git_workspace,
)
from ._models import (
    GitBranchesResponse,
    GitDiffResponse,
    GitLogEntry,
    WorkspaceGitResponse,
)

router = APIRouter()


# Cap on the patch body returned by GET /git/diff. The summary is still
# computed from the *untruncated* output (via `--numstat`), so the UI shows
# accurate insertion/deletion counts even when the patch is too big to ship.
_GIT_DIFF_PATCH_CAP = 256 * 1024


@router.get("/git", response_model=WorkspaceGitResponse)
async def api_workspace_git(request: Request, root: str) -> dict[str, Any]:
    await _require_known_root(request, root)
    mgr = require_sandbox_manager(request)
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
            status_code=503, detail=ErrorCode.WORKSPACE_SANDBOX_NOT_RUNNING.value
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
            raise HTTPException(
                status_code=400, detail=ErrorCode.WORKSPACE_PATH_MUST_BE_FILE.value
            )
        argv.extend(["--", rel])

    patch_code, patch_out, patch_err = await _drain_exec(
        mgr, handle, argv, cwd=WORKSPACE_MOUNT
    )
    if patch_code != 0:
        # Surface the stderr so the user can see e.g. "ambiguous argument".
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.WORKSPACE_GIT_COMMAND_FAILED.value,
                "params": {
                    "command": "diff",
                    "stderr": _decode(patch_err).strip(),
                },
            },
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
        raise HTTPException(
            status_code=400,
            detail={
                "code": ErrorCode.REQUEST_LIMIT_OUT_OF_RANGE.value,
                "params": {"min": 1, "max": 200},
            },
        )
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
            status_code=400,
            detail={
                "code": ErrorCode.WORKSPACE_GIT_COMMAND_FAILED.value,
                "params": {"command": "log", "stderr": _decode(err).strip()},
            },
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
