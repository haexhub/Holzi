"""Tests for the extended workspace-git surface (Plan 24).

Every endpoint runs through the FakeSandboxBackend: `script_exec` injects
scripted stdout/exit codes for each `git <op>` the route shells out to, and
`recorded_execs` asserts the exact argv. End-to-end push/pull against a real
git daemon is intentionally out of scope — the routes only orchestrate the
sandbox, so the contract that matters is `which git args do we invoke + how
do we shape the response from its output`."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes import config as hermes_config
from hermes.main import app
from hermes.sandbox import (
    ResourceLimits,
    SandboxManager,
)
from hermes.sandbox.fake import FakeSandboxBackend
from hermes.sandbox.models import ExecExit, ExecOutput

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}
WORKSPACE_MOUNT = "/workspace"


@pytest.fixture
async def client():
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


@pytest.fixture
def configure_roots(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(hermes_config.settings, "workspace_roots", value)

    return _set


@pytest.fixture
def destructive_on(monkeypatch):
    """Flip `HERMES_WORKSPACE_GIT_DESTRUCTIVE` on for tests that exercise
    the gated endpoints (discard, forced checkout)."""
    monkeypatch.setattr(
        hermes_config.settings, "workspace_git_destructive", True
    )


@pytest.fixture
async def install_sandbox():
    installed: list[SandboxManager] = []

    def _install() -> tuple[SandboxManager, FakeSandboxBackend]:
        backend = FakeSandboxBackend()
        mgr = SandboxManager(
            backend=backend,
            image="hermes-sandbox:test",
            network="none",
            default_limits=ResourceLimits(cpus=1.0, memory_mb=512, disk_mb=1024),
        )
        app.state.sandbox_manager = mgr
        installed.append(mgr)
        return mgr, backend

    yield _install

    for mgr in installed:
        await mgr.shutdown()
    app.state.sandbox_manager = None


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


# --- diff -----------------------------------------------------------------


async def test_diff_returns_text_patch_and_summary(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1&path=../etc/passwd", headers=AUTH
    )
    assert resp.status_code == 400


async def test_diff_truncates_large_patch(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
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
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/log?root=ws-1&limit=0", headers=AUTH
    )
    assert resp.status_code == 400
    resp = await client.get(
        "/api/workspace/git/log?root=ws-1&limit=500", headers=AUTH
    )
    assert resp.status_code == 400


# --- checkout -------------------------------------------------------------


async def test_checkout_succeeds_on_clean_tree(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    backend.script_exec(_stdout(""))  # status: clean
    backend.script_exec(_stdout(""))  # checkout

    resp = await client.post(
        "/api/workspace/git/checkout",
        json={"root": "ws-1", "branch": "feature/x"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    co = next(a for a in _git_argvs(backend) if a[1] == "checkout")
    assert co == ["git", "checkout", "feature/x"]


async def test_checkout_create_passes_dash_b(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))  # clean
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/checkout",
        json={"root": "ws-1", "branch": "topic", "create": True},
        headers=AUTH,
    )
    assert resp.status_code == 200
    co = next(a for a in _git_argvs(backend) if a[1] == "checkout")
    assert co == ["git", "checkout", "-b", "topic"]


async def test_checkout_dirty_returns_409(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(" M file.txt\n"))  # dirty

    resp = await client.post(
        "/api/workspace/git/checkout",
        json={"root": "ws-1", "branch": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 409


async def test_checkout_force_without_flag_returns_403(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))  # clean

    resp = await client.post(
        "/api/workspace/git/checkout",
        json={"root": "ws-1", "branch": "x", "force": True},
        headers=AUTH,
    )
    assert resp.status_code == 403


async def test_checkout_force_with_flag_passes_dash_f(
    client: httpx.AsyncClient, configure_roots, install_sandbox, destructive_on
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))  # clean
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/checkout",
        json={"root": "ws-1", "branch": "x", "force": True},
        headers=AUTH,
    )
    assert resp.status_code == 200
    co = next(a for a in _git_argvs(backend) if a[1] == "checkout")
    assert co == ["git", "checkout", "-f", "x"]


# --- stage / unstage / discard --------------------------------------------


async def test_stage_empty_paths_uses_dash_A(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/stage",
        json={"root": "ws-1", "paths": []},
        headers=AUTH,
    )
    assert resp.status_code == 200
    add = next(a for a in _git_argvs(backend) if a[1] == "add")
    assert add == ["git", "add", "-A"]


async def test_stage_explicit_paths(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/stage",
        json={"root": "ws-1", "paths": ["src/x.py", "README.md"]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    add = next(a for a in _git_argvs(backend) if a[1] == "add")
    assert add == ["git", "add", "--", "src/x.py", "README.md"]


async def test_stage_rejects_traversal_path(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.post(
        "/api/workspace/git/stage",
        json={"root": "ws-1", "paths": ["../etc/passwd"]},
        headers=AUTH,
    )
    assert resp.status_code == 400


async def test_unstage_empty_paths_uses_dot(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/unstage",
        json={"root": "ws-1", "paths": []},
        headers=AUTH,
    )
    assert resp.status_code == 200
    restore = next(a for a in _git_argvs(backend) if a[1] == "restore")
    assert restore == ["git", "restore", "--staged", "."]


async def test_discard_without_flag_returns_403(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.post(
        "/api/workspace/git/discard",
        json={"root": "ws-1", "paths": ["x.txt"], "conversation_id": "1"},
        headers=AUTH,
    )
    assert resp.status_code == 403


async def test_discard_with_flag_runs_checkout(
    client: httpx.AsyncClient, configure_roots, install_sandbox, destructive_on
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    backend.script_exec(_stdout(""))  # checkout --

    resp = await client.post(
        "/api/workspace/git/discard",
        json={"root": "ws-1", "paths": ["x.txt"], "conversation_id": "1"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    co = next(a for a in _git_argvs(backend) if a[1] == "checkout")
    assert co == ["git", "checkout", "--", "x.txt"]


# --- commit ---------------------------------------------------------------


async def test_commit_writes_message_with_identity_flags(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    backend.script_exec(_stdout(""))  # commit

    resp = await client.post(
        "/api/workspace/git/commit",
        json={
            "root": "ws-1",
            "message": "feat: thing",
            "conversation_id": "42",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    commit = next(a for a in _git_argvs(backend) if "commit" in a)
    # identity flags must precede the `commit` subcommand
    assert commit[1:5] == ["-c", "user.name=Holzi", "-c", "user.email=holzi@local"]
    msg = commit[commit.index("-m") + 1]
    assert msg.startswith("[user conv-42] feat: thing")


async def test_commit_all_inserts_dash_a(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/commit",
        json={
            "root": "ws-1",
            "message": "wip",
            "conversation_id": "1",
            "all": True,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    commit = next(a for a in _git_argvs(backend) if "commit" in a)
    assert "-a" in commit
    # `-a` must come immediately after `commit`
    assert commit[commit.index("commit") + 1] == "-a"


async def test_commit_propagates_git_error(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_fail("nothing to commit, working tree clean"))

    resp = await client.post(
        "/api/workspace/git/commit",
        json={"root": "ws-1", "message": "x", "conversation_id": "1"},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert "nothing to commit" in resp.json()["detail"]


# --- fetch / pull / push --------------------------------------------------


async def test_fetch_success_returns_ok(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/fetch",
        json={"root": "ws-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_fetch_failure_returns_ok_false_with_stderr(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_fail("fatal: Could not read from remote repository."))

    resp = await client.post(
        "/api/workspace/git/fetch",
        json={"root": "ws-1"},
        headers=AUTH,
    )
    # Push/pull/fetch surface remote failures as 200 ok=false so the UI
    # doesn't have to read 4xx bodies just to learn "no remote configured".
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Could not read from remote" in body["message"]


async def test_pull_clean_returns_ok(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout("Already up to date.\n"))

    resp = await client.post(
        "/api/workspace/git/pull",
        json={"root": "ws-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["conflicts"] == []


async def test_pull_conflict_returns_file_list_not_500(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(
        [
            ExecOutput(
                stream="stdout",
                data=(
                    b"Auto-merging README.md\n"
                    b"CONFLICT (content): Merge conflict in README.md\n"
                    b"CONFLICT (content): Merge conflict in src/x.py\n"
                ),
            ),
            ExecOutput(
                stream="stderr",
                data=b"Automatic merge failed; fix conflicts and then commit.\n",
            ),
            ExecExit(exit_code=1),
        ]
    )
    # The pull endpoint enumerates unmerged paths via
    # `git diff --name-only --diff-filter=U` (not from the CONFLICT lines).
    backend.script_exec(_stdout("README.md\nsrc/x.py\n"))

    resp = await client.post(
        "/api/workspace/git/pull",
        json={"root": "ws-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert sorted(body["conflicts"]) == ["README.md", "src/x.py"]
    assert "merge failed" in body["message"].lower()


async def test_push_success(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout(""))

    resp = await client.post(
        "/api/workspace/git/push",
        json={"root": "ws-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    push = next(a for a in _git_argvs(backend) if a[1] == "push")
    assert push == ["git", "push"]


async def test_push_set_upstream_resolves_current_branch(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse is-inside-work-tree
    backend.script_exec(_stdout("feature/x\n"))  # rev-parse --abbrev-ref HEAD
    backend.script_exec(_stdout(""))  # push

    resp = await client.post(
        "/api/workspace/git/push",
        json={"root": "ws-1", "set_upstream": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    push = next(a for a in _git_argvs(backend) if a[1] == "push")
    assert push == ["git", "push", "-u", "origin", "feature/x"]


async def test_push_set_upstream_from_detached_head_400(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])
    backend.script_exec(_stdout("HEAD\n"))  # detached

    resp = await client.post(
        "/api/workspace/git/push",
        json={"root": "ws-1", "set_upstream": True},
        headers=AUTH,
    )
    assert resp.status_code == 400


# --- shared 4xx behaviour --------------------------------------------------


async def test_diff_requires_known_root(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-unknown", headers=AUTH
    )
    assert resp.status_code == 404


async def test_diff_503_when_sandbox_not_configured(
    client: httpx.AsyncClient, configure_roots
) -> None:
    configure_roots("ws-1")
    # No install_sandbox → app.state.sandbox_manager stays None.
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1", headers=AUTH
    )
    assert resp.status_code == 503


# --- review fixes ---------------------------------------------------------


@pytest.mark.parametrize(
    "branch",
    ["--orphan", "-B", "--detach", "--track=foo"],
)
async def test_checkout_rejects_flag_like_branch_name(
    client: httpx.AsyncClient, configure_roots, install_sandbox, branch: str
) -> None:
    """`git checkout --orphan x` is destructive and bypasses the dirty-tree
    gate; reject any branch name starting with `-` before it ever reaches
    the argv."""
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.post(
        "/api/workspace/git/checkout",
        json={"root": "ws-1", "branch": branch},
        headers=AUTH,
    )
    assert resp.status_code == 400, branch


@pytest.mark.parametrize(
    "bad_path", ["-n", "--hard", "-rf"],
)
async def test_stage_rejects_flag_like_path(
    client: httpx.AsyncClient, configure_roots, install_sandbox, bad_path: str
) -> None:
    """Every git endpoint that takes paths today routes them through `--`
    in the argv, so a `-`-prefixed path can't hit `git add`. We harden at
    the normaliser anyway so a future caller that forgets the `--` doesn't
    get flag-injection for free."""
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.post(
        "/api/workspace/git/stage",
        json={"root": "ws-1", "paths": [bad_path]},
        headers=AUTH,
    )
    assert resp.status_code == 400, bad_path


async def test_diff_rejects_flag_like_path(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    resp = await client.get(
        "/api/workspace/git/diff?root=ws-1&path=-n", headers=AUTH
    )
    assert resp.status_code == 400


async def test_pull_conflict_uses_diff_name_only_not_line_parser(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    """`git diff --name-only --diff-filter=U` is authoritative, so an
    "added in both" conflict (which a naive `CONFLICT … in <path>` grep
    would point at the branch name instead of the file) still ends up
    listing the correct path."""
    configure_roots("ws-1")
    _, backend = install_sandbox()
    backend.script_exec([ExecExit(exit_code=0)])  # rev-parse
    backend.script_exec(
        [
            ExecOutput(
                stream="stdout",
                data=(
                    # CONFLICT line with branch name after ` in ` — a naive
                    # parser would have extracted "feature/x" as the file.
                    b"CONFLICT (add/add): src/dup.py added in HEAD and added in feature/x\n"
                ),
            ),
            ExecOutput(
                stream="stderr",
                data=b"Automatic merge failed; fix conflicts and then commit.\n",
            ),
            ExecExit(exit_code=1),
        ],
    )
    backend.script_exec(_stdout("src/dup.py\n"))

    resp = await client.post(
        "/api/workspace/git/pull",
        json={"root": "ws-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["conflicts"] == ["src/dup.py"]
    name_only_call = next(
        a for a in _git_argvs(backend) if a[1] == "diff" and "--diff-filter=U" in a
    )
    assert name_only_call == [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=U",
    ]
