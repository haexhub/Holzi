"""Tests for the read-only workspace-git inspection endpoints (Plan 24).

Every endpoint runs through the FakeSandboxBackend: `script_exec` injects
scripted stdout/exit codes for each `git <op>` the route shells out to, and
`recorded_execs` asserts the exact argv. End-to-end push/pull against a real
git daemon is intentionally out of scope — the routes only orchestrate the
sandbox, so the contract that matters is `which git args do we invoke + how
do we shape the response from its output`."""

from __future__ import annotations

import httpx
import pytest

from hermes import config as hermes_config
from hermes.sandbox.fake import FakeSandboxBackend
from hermes.sandbox.models import ExecExit, ExecOutput

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}
WORKSPACE_MOUNT = "/workspace"




@pytest.fixture
def destructive_on(monkeypatch):
    """Flip `HERMES_WORKSPACE_GIT_DESTRUCTIVE` on for tests that exercise
    the gated endpoints (discard, forced checkout)."""
    monkeypatch.setattr(
        hermes_config.settings, "workspace_git_destructive", True
    )




def _git_argvs(backend: FakeSandboxBackend) -> list[list[str]]:
    return [a for a in backend.recorded_execs if a and a[0] == "git"]


def _stdout(data: str) -> list:
    return [
        ExecOutput(stream="stdout", data=data.encode("utf-8")),
        ExecExit(exit_code=0),
    ]


def _fail(stderr: str, *, exit_code: int = 1) -> list:
    return [
        ExecOutput(stream="stderr", data=stderr.encode("utf-8")),
        ExecExit(exit_code=exit_code),
    ]


# --- git status -----------------------------------------------------------


async def test_git_status_reports_not_a_repo(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    await mgr.get_workspace("ws-1")
    backend.script_exec([ExecExit(exit_code=128)])  # rev-parse fails
    response = await client.get(
        "/api/workspace/git",
        params={"root": "ws-1"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "root": "ws-1",
        "is_repo": False,
        "branch": None,
        "dirty": False,
        "entries": [],
    }


async def test_git_status_branch_and_dirty_entries(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    await mgr.get_workspace("ws-1")
    # 1) rev-parse --is-inside-work-tree → exit 0 (is a repo)
    backend.script_exec([ExecOutput("stdout", b"true\n"), ExecExit(exit_code=0)])
    # 2) rev-parse --abbrev-ref HEAD → "main"
    backend.script_exec([ExecOutput("stdout", b"main\n"), ExecExit(exit_code=0)])
    # 3) status --porcelain=v1 → two entries
    backend.script_exec(
        [
            ExecOutput("stdout", b" M src/foo.py\n?? new.md\n"),
            ExecExit(exit_code=0),
        ]
    )
    response = await client.get(
        "/api/workspace/git",
        params={"root": "ws-1"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_repo"] is True
    assert body["branch"] == "main"
    assert body["dirty"] is True
    assert {(e["status"], e["path"]) for e in body["entries"]} == {
        (" M", "src/foo.py"),
        ("??", "new.md"),
    }


async def test_git_status_clean_repo(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    await mgr.get_workspace("ws-1")
    backend.script_exec([ExecOutput("stdout", b"true\n"), ExecExit(exit_code=0)])
    backend.script_exec(
        [ExecOutput("stdout", b"feature/x\n"), ExecExit(exit_code=0)]
    )
    backend.script_exec([ExecExit(exit_code=0)])  # empty porcelain
    response = await client.get(
        "/api/workspace/git",
        params={"root": "ws-1"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["branch"] == "feature/x"
    assert body["dirty"] is False
    assert body["entries"] == []


async def test_git_503_when_sandbox_unconfigured(
    client: httpx.AsyncClient, configure_workspaces
) -> None:
    await configure_workspaces(["ws-1"])
    response = await client.get(
        "/api/workspace/git",
        params={"root": "ws-1"},
        headers=AUTH,
    )
    assert response.status_code == 503


# --- diff -----------------------------------------------------------------


async def test_diff_returns_text_patch_and_summary(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    patch_body = (
        "diff --git a/x.txt b/x.txt\n"
        "--- a/x.txt\n"
        "+++ b/x.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    # FIFO: rev-parse (is-inside-work-tree, default), diff (patch), diff --numstat.
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    backend.script_exec(_stdout(patch_body))  # git diff
    backend.script_exec(_stdout("1\t1\tx.txt\n"))  # numstat

    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1", headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "text"
    assert body["patch"] == patch_body
    assert body["summary"] == {"files": 1, "insertions": 1, "deletions": 1}
    assert body["truncated"] is False


async def test_diff_returns_none_when_clean(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    backend.script_exec(_stdout(""))  # git diff (empty)
    backend.script_exec(_stdout(""))  # numstat (empty)

    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "none"
    assert body["patch"] is None
    assert body["summary"] == {"files": 0, "insertions": 0, "deletions": 0}


async def test_diff_binary_reports_kind_binary(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    backend.script_exec(_stdout(""))  # diff (no body for binary)
    backend.script_exec(_stdout("-\t-\timage.png\n"))  # numstat marks binary

    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1", headers=AUTH
    )
    body = resp.json()
    assert body["kind"] == "binary"
    assert body["patch"] is None
    assert body["summary"]["files"] == 1


async def test_diff_staged_passes_flag(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))
    backend.script_exec(_stdout(""))

    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1&staged=true", headers=AUTH
    )
    assert resp.status_code == 200
    diffs = [a for a in _git_argvs(backend) if a[1] == "diff" and "--staged" in a]
    # one patch + one numstat → both should carry --staged
    assert len(diffs) == 2


async def test_diff_with_path_restricts_argv(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))
    backend.script_exec(_stdout(""))

    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1&path=src/x.py", headers=AUTH
    )
    assert resp.status_code == 200
    patch_argv = next(a for a in _git_argvs(backend) if a[1] == "diff" and "--numstat" not in a)
    assert patch_argv[-2:] == ["--", "src/x.py"]


async def test_diff_rejects_traversal_path(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1&path=../etc/passwd", headers=AUTH
    )
    assert resp.status_code == 400


async def test_diff_truncates_large_patch(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    big = "+" + "x" * (300 * 1024)
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(big))
    backend.script_exec(_stdout("1\t0\tfile\n"))

    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1", headers=AUTH
    )
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["patch"]) == 256 * 1024


# --- branches -------------------------------------------------------------


async def test_branches_lists_local_and_remote(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # is-inside-work-tree
    backend.script_exec(_stdout("main\n"))  # rev-parse --abbrev-ref HEAD
    backend.script_exec(
        _stdout(
            "main|2026-05-31T10:00:00+02:00|refs/heads/main\n"
            "feature/x|2026-05-30T08:00:00+02:00|refs/heads/feature/x\n"
            "origin/main|2026-05-31T09:55:00+02:00|refs/remotes/origin/main\n"
            "origin/HEAD|2026-05-31T09:55:00+02:00|refs/remotes/origin/HEAD\n"
        )
    )

    resp = await client.get(
        "/api/workspace/git/branches?root=ws-1", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "main"
    names = [b["name"] for b in body["all"]]
    assert names == ["main", "feature/x", "origin/main"]  # origin/HEAD filtered
    assert body["all"][2]["is_remote"] is True


async def test_branches_detached_head_reports_null_current(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout("HEAD\n"))  # detached
    backend.script_exec(_stdout(""))

    resp = await client.get(
        "/api/workspace/git/branches?root=ws-1", headers=AUTH
    )
    assert resp.json()["current"] is None


# --- log ------------------------------------------------------------------


async def test_log_parses_unit_separator_format(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    sha = "abcdef1234567890" * 2 + "abcdef12"  # 40 chars
    line = (
        sha + "\x1fabcdef12\x1fAlice\x1f2026-05-31T12:00:00+02:00\x1ffix | pipe in subject\n"
    )
    backend.script_exec(_stdout(line))

    resp = await client.get(
        "/api/workspace/git/log?root=ws-1&limit=10", headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["short_sha"] == "abcdef12"
    assert body[0]["subject"] == "fix | pipe in subject"


async def test_log_limit_clamped(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/log?root=ws-1&limit=0", headers=AUTH
    )
    assert resp.status_code == 400
    resp = await client.get(
        "/api/workspace/git/log?root=ws-1&limit=500", headers=AUTH
    )
    assert resp.status_code == 400


# --- shared 4xx behaviour --------------------------------------------------


async def test_diff_requires_known_root(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-unknown", headers=AUTH
    )
    assert resp.status_code == 404


async def test_diff_503_when_sandbox_not_configured(
    client: httpx.AsyncClient, configure_workspaces
) -> None:
    await configure_workspaces(["ws-1"])
    # No install_sandbox → app.state.sandbox_manager stays None.
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1", headers=AUTH
    )
    assert resp.status_code == 503


# --- review fixes ---------------------------------------------------------


async def test_diff_rejects_flag_like_path(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1&path=-n", headers=AUTH
    )
    assert resp.status_code == 400
