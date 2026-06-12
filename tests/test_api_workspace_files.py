"""Tests for the workspace file-write endpoints (Plan 13)."""

from __future__ import annotations

import struct
import zlib

import httpx
import pytest

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}

WORKSPACE_MOUNT = "/workspace"
TEXT_PREVIEW_CAP = 256 * 1024
IMAGE_PREVIEW_CAP = 2 * 1024 * 1024




# Tiny valid PNG (1x1 transparent) for image-preview tests. Built inline so
# the test file stays self-contained.
def _tiny_png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# --- Plan 13: create file ---------------------------------------------------


async def test_file_create_writes_and_commits(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    response = await client.post(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "src/new.py",
            "content": "x = 1\n",
            "conversation_id": "42",
        },
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["root"] == "ws-1"
    assert body["path"] == "src/new.py"
    assert body["committed"] is True
    # File actually landed in the sandbox volume.
    handle = await mgr.get_workspace("ws-1")
    assert (
        await mgr.read_file(handle, f"{WORKSPACE_MOUNT}/src/new.py")
        == b"x = 1\n"
    )
    # The `git commit -m` call carries the user[conv-N]: tag.
    commit_argvs = [
        a for a in backend.recorded_execs if a and a[0] == "git" and "commit" in a
    ]
    assert commit_argvs, "no git commit call recorded"
    message = commit_argvs[-1][commit_argvs[-1].index("-m") + 1]
    assert message.startswith("user[conv-42]:")
    assert "create" in message
    assert "src/new.py" in message


async def test_file_create_409_when_exists(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/x.txt", b"old")
    response = await client.post(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "x.txt",
            "content": "new",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 409


async def test_file_create_in_fresh_subdir_succeeds(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    """`write_file` mkdir-p's the parent, so creating a file inside a
    not-yet-existing subdir is the expected happy path — the panel UI can
    target a fresh directory in one shot."""
    await configure_workspaces(["ws-1"])
    mgr, _ = install_sandbox()
    response = await client.post(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "deep/nested/x.txt",
            "content": "y",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    handle = await mgr.get_workspace("ws-1")
    assert (
        await mgr.read_file(handle, f"{WORKSPACE_MOUNT}/deep/nested/x.txt")
        == b"y"
    )


@pytest.mark.parametrize(
    "bad_path", ["..", "a/../b", "/etc/passwd", "/", "a//b", "./x"]
)
async def test_file_create_rejects_traversal(
    client: httpx.AsyncClient,
    configure_workspaces,
    install_sandbox,
    bad_path: str,
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.post(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": bad_path,
            "content": "x",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 400, bad_path


async def test_file_create_rejects_binary_content(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.post(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "blob.bin",
            "content": "hello\x00world",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 400


async def test_file_create_committed_false_when_not_a_repo(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    """When `git rev-parse --is-inside-work-tree` exits non-zero the helper
    no-ops the commit — the file write still went through."""
    from hermes.sandbox.models import ExecExit

    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    # Pre-warm the sandbox so the rev-parse call is the first scripted exec.
    await mgr.get_workspace("ws-1")
    backend.script_exec([ExecExit(exit_code=128)])  # rev-parse fails
    response = await client.post(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "loose.txt",
            "content": "no repo here",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    assert response.json()["committed"] is False
    # No `git add` / `git commit` followed once rev-parse said "not a repo".
    git_argvs = [a for a in backend.recorded_execs if a and a[0] == "git"]
    # Only the rev-parse should have run.
    assert all(a[1] == "rev-parse" for a in git_argvs)


# --- Plan 13: update file with base_sha -------------------------------------


async def test_file_update_succeeds_with_matching_base_sha(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    import hashlib as _hl

    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    initial = b"v1\n"
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/file.py", initial)
    response = await client.put(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "file.py",
            "content": "v2\n",
            "base_sha": _hl.sha256(initial).hexdigest(),
            "conversation_id": "7",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sha256"] == _hl.sha256(b"v2\n").hexdigest()
    assert body["committed"] is True
    assert await mgr.read_file(handle, f"{WORKSPACE_MOUNT}/file.py") == b"v2\n"
    commit_argv = [
        a for a in backend.recorded_execs if a and a[0] == "git" and "commit" in a
    ][-1]
    message = commit_argv[commit_argv.index("-m") + 1]
    assert message.startswith("user[conv-7]:")
    assert "edit" in message
    assert "file.py" in message


async def test_file_update_409_on_base_sha_mismatch(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/file.py", b"current\n")
    response = await client.put(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "file.py",
            "content": "x",
            "base_sha": "0" * 64,  # not the real sha
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 409
    # The file is unchanged on disk.
    assert (
        await mgr.read_file(handle, f"{WORKSPACE_MOUNT}/file.py")
        == b"current\n"
    )


async def test_file_update_422_on_malformed_base_sha(
    client: httpx.AsyncClient,
) -> None:
    # base_sha is compared against a sha256 hexdigest; a non-hex value is a
    # bad request (422 at validation), not an edit conflict (409).
    response = await client.put(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "file.py",
            "content": "x",
            "base_sha": "not-a-valid-sha",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 422


async def test_file_update_404_when_missing(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.put(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "missing.txt",
            "content": "x",
            "base_sha": "0" * 64,
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 404


async def test_file_update_rejects_binary_content(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    import hashlib as _hl

    await configure_workspaces(["ws-1"])
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    initial = b"text\n"
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/text.txt", initial)
    response = await client.put(
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "text.txt",
            "content": "with\x00nul",
            "base_sha": _hl.sha256(initial).hexdigest(),
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 400


# --- Plan 13: rename --------------------------------------------------------


async def test_file_rename_moves_and_commits(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/old.md", b"# title\n")
    response = await client.post(
        "/api/workspace/rename",
        json={
            "root": "ws-1",
            "src": "old.md",
            "dest": "new.md",
            "conversation_id": "9",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["committed"] is True
    # `mv` was actually invoked in the sandbox.
    mv_argvs = [a for a in backend.recorded_execs if a[:2] == ["mv", "--"]]
    assert mv_argvs
    commit_argv = [
        a for a in backend.recorded_execs if a and a[0] == "git" and "commit" in a
    ][-1]
    message = commit_argv[commit_argv.index("-m") + 1]
    assert message.startswith("user[conv-9]:")
    assert "rename" in message


async def test_file_rename_409_when_dest_exists(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/a.txt", b"a")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/b.txt", b"b")
    response = await client.post(
        "/api/workspace/rename",
        json={
            "root": "ws-1",
            "src": "a.txt",
            "dest": "b.txt",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 409


async def test_file_rename_404_when_src_missing(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.post(
        "/api/workspace/rename",
        json={
            "root": "ws-1",
            "src": "nope.txt",
            "dest": "new.txt",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 404


async def test_file_rename_404_when_parent_missing(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    """Regression: a missing *parent* directory used to surface
    SandboxFileNotFound from `_stat_entry` as a 500. Should be a 404."""
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.post(
        "/api/workspace/rename",
        json={
            "root": "ws-1",
            "src": "missing/dir/a.txt",
            "dest": "b.txt",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 404


async def test_file_rename_rejects_traversal(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/a.txt", b"a")
    response = await client.post(
        "/api/workspace/rename",
        json={
            "root": "ws-1",
            "src": "a.txt",
            "dest": "../escape.txt",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 400


# --- Plan 13: delete --------------------------------------------------------


async def test_file_delete_removes_and_commits(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    mgr, backend = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/gone.txt", b"bye")
    response = await client.request(
        "DELETE",
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "gone.txt",
            "conversation_id": "3",
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["committed"] is True
    rm_argvs = [a for a in backend.recorded_execs if a[:2] == ["rm", "--"]]
    assert rm_argvs
    commit_argv = [
        a for a in backend.recorded_execs if a and a[0] == "git" and "commit" in a
    ][-1]
    message = commit_argv[commit_argv.index("-m") + 1]
    assert message.startswith("user[conv-3]:")
    assert "delete" in message


async def test_file_delete_404_when_missing(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.request(
        "DELETE",
        "/api/workspace/file",
        json={"root": "ws-1", "path": "no.txt", "conversation_id": "1"},
        headers=AUTH,
    )
    assert response.status_code == 404


async def test_file_delete_404_when_parent_missing(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    """Regression: missing parent dir on delete used to bubble
    SandboxFileNotFound as 500; expected 404."""
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.request(
        "DELETE",
        "/api/workspace/file",
        json={
            "root": "ws-1",
            "path": "missing/dir/x.txt",
            "conversation_id": "1",
        },
        headers=AUTH,
    )
    assert response.status_code == 404


async def test_file_delete_rejects_traversal(
    client: httpx.AsyncClient, configure_workspaces, install_sandbox
) -> None:
    await configure_workspaces(["ws-1"])
    install_sandbox()
    response = await client.request(
        "DELETE",
        "/api/workspace/file",
        json={"root": "ws-1", "path": "..", "conversation_id": "1"},
        headers=AUTH,
    )
    assert response.status_code == 400
