"""Tests for the read-only workspace browser endpoints (Plan 12)."""

from __future__ import annotations

import base64
import struct
import zlib

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

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}

WORKSPACE_MOUNT = "/workspace"
TEXT_PREVIEW_CAP = 256 * 1024
IMAGE_PREVIEW_CAP = 2 * 1024 * 1024


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
    """Set HERMES_WORKSPACE_ROOTS-equivalent in-process and tear it down."""

    def _set(value: str) -> None:
        monkeypatch.setattr(
            hermes_config.settings, "workspace_roots", value
        )

    return _set


@pytest.fixture
async def install_sandbox():
    """Install a FakeSandboxBackend-backed manager on app.state and tear it
    down so the next test starts with the default `None` manager."""
    installed: list[SandboxManager] = []

    def _install() -> tuple[SandboxManager, FakeSandboxBackend]:
        backend = FakeSandboxBackend()
        mgr = SandboxManager(
            backend=backend,
            image="hermes-sandbox:test",
            network="none",
            default_limits=ResourceLimits(
                cpus=1.0, memory_mb=512, disk_mb=1024
            ),
        )
        app.state.sandbox_manager = mgr
        installed.append(mgr)
        return mgr, backend

    yield _install

    for mgr in installed:
        await mgr.shutdown()
    app.state.sandbox_manager = None


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


# --- auth + configuration ---------------------------------------------------


async def test_roots_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/workspace/roots")
    assert response.status_code == 401


async def test_roots_empty_when_unconfigured(
    client: httpx.AsyncClient, configure_roots
) -> None:
    configure_roots("")
    response = await client.get("/api/workspace/roots", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"roots": []}


async def test_roots_lists_configured_ids(
    client: httpx.AsyncClient, configure_roots
) -> None:
    configure_roots("ws-1, ws-2 ,,")
    response = await client.get("/api/workspace/roots", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"roots": [{"id": "ws-1"}, {"id": "ws-2"}]}


async def test_tree_503_when_sandbox_not_configured(
    client: httpx.AsyncClient, configure_roots
) -> None:
    configure_roots("ws-1")
    assert app.state.sandbox_manager is None
    response = await client.get(
        "/api/workspace/tree", params={"root": "ws-1"}, headers=AUTH
    )
    assert response.status_code == 503


async def test_tree_unknown_root_returns_404(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    response = await client.get(
        "/api/workspace/tree", params={"root": "ws-other"}, headers=AUTH
    )
    assert response.status_code == 404


# --- path traversal ---------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    ["..", "a/../b", "/etc", "/", "/abs/path", "a//b", "./a", "a/./b"],
)
async def test_tree_rejects_traversal_paths(
    client: httpx.AsyncClient,
    configure_roots,
    install_sandbox,
    bad_path: str,
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    response = await client.get(
        "/api/workspace/tree",
        params={"root": "ws-1", "path": bad_path},
        headers=AUTH,
    )
    assert response.status_code == 400, bad_path


# --- tree happy paths -------------------------------------------------------


async def test_tree_empty_workspace_returns_empty_entries(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    """A freshly-started workspace has no files yet but `/workspace` itself is
    a mounted volume in real Podman — the API must surface that as 200 with an
    empty entries list, not as 404 (the frontend renders that as "empty
    workspace", not "missing workspace")."""
    configure_roots("ws-1")
    install_sandbox()
    response = await client.get(
        "/api/workspace/tree",
        params={"root": "ws-1", "path": ""},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["root"] == "ws-1"
    assert body["path"] == ""
    assert body["entries"] == []


async def test_tree_lists_files_and_dirs(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/readme.md", b"# hello")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/src/main.py", b"print(1)")

    response = await client.get(
        "/api/workspace/tree",
        params={"root": "ws-1", "path": ""},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["root"] == "ws-1"
    assert body["path"] == ""
    by_name = {e["name"]: e for e in body["entries"]}
    assert by_name["readme.md"]["type"] == "file"
    assert by_name["readme.md"]["size"] == len(b"# hello")
    assert by_name["src"]["type"] == "dir"


async def test_tree_nested_path(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/src/a.py", b"x = 1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/src/b.py", b"x = 22")

    response = await client.get(
        "/api/workspace/tree",
        params={"root": "ws-1", "path": "src"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "src"
    names = sorted(e["name"] for e in body["entries"])
    assert names == ["a.py", "b.py"]


async def test_tree_on_file_returns_400(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/file.txt", b"hi")
    response = await client.get(
        "/api/workspace/tree",
        params={"root": "ws-1", "path": "file.txt"},
        headers=AUTH,
    )
    assert response.status_code == 400


# --- file preview ----------------------------------------------------------


async def test_file_503_when_sandbox_not_configured(
    client: httpx.AsyncClient, configure_roots
) -> None:
    configure_roots("ws-1")
    assert app.state.sandbox_manager is None
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "x.txt"},
        headers=AUTH,
    )
    assert response.status_code == 503


async def test_file_unknown_root_404(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-other", "path": "x.txt"},
        headers=AUTH,
    )
    assert response.status_code == 404


async def test_file_missing_returns_404(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/other.txt", b"hi")
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "nope.txt"},
        headers=AUTH,
    )
    assert response.status_code == 404


async def test_file_on_directory_returns_400(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/src/a.py", b"x = 1")
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "src"},
        headers=AUTH,
    )
    assert response.status_code == 400


async def test_file_empty_path_returns_400(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    install_sandbox()
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": ""},
        headers=AUTH,
    )
    assert response.status_code == 400


async def test_file_text_preview(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    text = "hello\nworld\n"
    await mgr.write_file(
        handle, f"{WORKSPACE_MOUNT}/notes.txt", text.encode()
    )
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "notes.txt"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "text"
    assert body["content"] == text
    assert body["truncated"] is False
    assert body["data_url"] is None
    assert body["size"] == len(text.encode())


async def test_file_markdown_preview(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    md = "# Title\n\nBody.\n"
    await mgr.write_file(
        handle, f"{WORKSPACE_MOUNT}/README.md", md.encode()
    )
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "README.md"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "markdown"
    assert body["content"] == md


async def test_tree_on_dead_sandbox_returns_503(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    """A crashed workspace must surface as 503 (matching the chat-stream's
    crash semantics) so the frontend can offer Restart via the same path,
    not 500 (which would imply an internal bug)."""
    configure_roots("ws-1")
    mgr, backend = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    backend.simulate_crash(handle.id)

    response = await client.get(
        "/api/workspace/tree",
        params={"root": "ws-1", "path": ""},
        headers=AUTH,
    )
    assert response.status_code == 503


async def test_file_on_dead_sandbox_returns_503(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, backend = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/x.txt", b"hi")
    backend.simulate_crash(handle.id)

    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "x.txt"},
        headers=AUTH,
    )
    assert response.status_code == 503


async def test_file_svg_previews_as_text_source(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    """SVG is XML — we'd rather show the source than base64-inline an opaque
    image, so it falls into the text path (not image)."""
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    svg = '<svg xmlns="http://www.w3.org/2000/svg"/>\n'
    await mgr.write_file(
        handle, f"{WORKSPACE_MOUNT}/icon.svg", svg.encode()
    )
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "icon.svg"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "text"
    assert body["content"] == svg
    assert body["data_url"] is None


async def test_file_binary_metadata_only(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    # A NUL early in the file triggers the binary classifier.
    data = b"abc\x00def" + b"X" * 100
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/blob.bin", data)
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "blob.bin"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "binary"
    assert body["content"] is None
    assert body["data_url"] is None
    assert body["size"] == len(data)


async def test_file_image_preview(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    png = _tiny_png()
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/pic.png", png)
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "pic.png"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "image"
    assert body["content"] is None
    assert body["data_url"] is not None
    assert body["data_url"].startswith("data:image/png;base64,")
    decoded = base64.b64decode(body["data_url"].split(",", 1)[1])
    assert decoded == png


async def test_file_oversized_text_is_truncated(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    huge = ("a" * (TEXT_PREVIEW_CAP + 100)).encode()
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/big.txt", huge)
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "big.txt"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "text"
    assert body["truncated"] is True
    assert len(body["content"]) == TEXT_PREVIEW_CAP


async def test_file_oversized_image_returns_metadata_only(
    client: httpx.AsyncClient, configure_roots, install_sandbox
) -> None:
    configure_roots("ws-1")
    mgr, _ = install_sandbox()
    handle = await mgr.get_workspace("ws-1")
    # The runtime cap is 10 MiB; the image preview cap is 2 MiB, so a 3 MiB
    # "image" is over the API cap but under the runtime cap.
    fake_png = b"\x89PNG" + b"\x00" * (IMAGE_PREVIEW_CAP + 1)
    await mgr.write_file(handle, f"{WORKSPACE_MOUNT}/huge.png", fake_png)
    response = await client.get(
        "/api/workspace/file",
        params={"root": "ws-1", "path": "huge.png"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "image"
    assert body["data_url"] is None
    assert body["content"] is None
    assert body["size"] == len(fake_png)
