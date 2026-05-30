"""End-to-end tests for the Plan-25 multi-workspace CRUD surface.

Backend tests focus on the route contract (auth, slug validation, idempotent
backfill, 404/409/400 mappings) and the disk / git probe aggregation. The
sandbox aggregation uses the in-memory `FakeSandboxBackend` so we never
require a real Podman runtime in CI — the probes get scripted output the
same way the existing workspace-write tests do.
"""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes import config as hermes_config
from hermes.main import app
from hermes.repository import workspaces as repo
from hermes.repository.workspaces import validate_slug
from hermes.sandbox import (
    ExecExit,
    ResourceLimits,
    SandboxManager,
)
from hermes.sandbox.fake import FakeSandboxBackend
from hermes.sandbox.models import ExecOutput

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


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
async def install_sandbox():
    """Install a FakeSandboxBackend-backed manager on app.state and tear it
    down so the next test starts with the default `None` manager.

    Borrowed verbatim from `test_api_workspace.py` — keeping the duplication
    is cheaper than abstracting; the two test modules cover different
    surfaces and the fixture is six lines."""
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


# --- slug validation -------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["ws", "ws-1", "my-project", "ab", "01-prefixed", "a-" + "b" * 62],
)
def test_validate_slug_accepts(slug: str) -> None:
    validate_slug(slug)  # no raise


@pytest.mark.parametrize(
    "slug",
    [
        "",  # empty
        "a",  # too short (1 char)
        "-leading-dash",
        "trailing-dash-",
        "ABC",  # uppercase
        "with_underscore",
        "with space",
        "with/slash",
        "a" * 65,  # too long
    ],
)
def test_validate_slug_rejects(slug: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(slug)


# --- auth ------------------------------------------------------------------


async def test_workspaces_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/workspaces")
    assert response.status_code == 401


# --- CRUD round-trip -------------------------------------------------------


async def test_create_workspace_returns_aggregated_row(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-test", "display_name": "Test Workspace"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "ws-test"
    assert body["display_name"] == "Test Workspace"
    assert isinstance(body["created_at"], int)
    assert body["archived_at"] is None
    # Without an installed sandbox manager the aggregate row reports absent
    # sandbox + null disk + non-repo git — operators on a sandbox-less host
    # still get a usable list.
    assert body["sandbox"] == {"state": "absent", "exit_code": None}
    assert body["disk"] == {"used_mb": None, "quota_mb": None}
    assert body["git"] == {"is_repo": False, "branch": None, "dirty": False}


async def test_list_workspaces_excludes_archived(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-keep", "display_name": "Keep"},
    )
    await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-archive", "display_name": "Archive"},
    )
    archive_resp = await client.delete(
        "/api/workspaces/ws-archive", headers=AUTH
    )
    assert archive_resp.status_code == 204

    list_resp = await client.get("/api/workspaces", headers=AUTH)
    assert list_resp.status_code == 200
    ids = [r["id"] for r in list_resp.json()]
    assert ids == ["ws-keep"]


async def test_create_workspace_rejects_invalid_slug(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "Invalid Slug", "display_name": "Whatever"},
    )
    # FastAPI's body validation catches min_length and the route turns the
    # repo-layer ValueError into a 400 for shape-violations the validator
    # lets through. Either way the response is in the 4xx range.
    assert response.status_code in (400, 422)


async def test_create_workspace_409_on_duplicate(
    client: httpx.AsyncClient,
) -> None:
    first = await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-dup", "display_name": "First"},
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-dup", "display_name": "Second"},
    )
    assert second.status_code == 409


async def test_rename_workspace_updates_display_name(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-rename", "display_name": "Old Name"},
    )
    patch = await client.patch(
        "/api/workspaces/ws-rename",
        headers=AUTH,
        json={"display_name": "New Name"},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["display_name"] == "New Name"
    # And the list view picks up the new label too.
    body = (await client.get("/api/workspaces", headers=AUTH)).json()
    by_id = {r["id"]: r for r in body}
    assert by_id["ws-rename"]["display_name"] == "New Name"


async def test_rename_unknown_404(client: httpx.AsyncClient) -> None:
    patch = await client.patch(
        "/api/workspaces/no-such-ws",
        headers=AUTH,
        json={"display_name": "Whatever"},
    )
    assert patch.status_code == 404


async def test_archive_unknown_404(client: httpx.AsyncClient) -> None:
    response = await client.delete(
        "/api/workspaces/no-such-ws", headers=AUTH
    )
    assert response.status_code == 404


async def test_disk_endpoint_404_for_unknown(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/workspaces/no-such-ws/disk", headers=AUTH
    )
    assert response.status_code == 404


# --- env backfill ----------------------------------------------------------


async def test_backfill_from_env_is_idempotent(
    client: httpx.AsyncClient, monkeypatch,
) -> None:
    """The lifespan-time backfill is idempotent — running it twice from the
    repository must not duplicate rows."""
    db = app.state.db
    inserted = await repo.backfill_from_env(db, slugs=["ws-a", "ws-b"])
    assert sorted(inserted) == ["ws-a", "ws-b"]
    again = await repo.backfill_from_env(db, slugs=["ws-a", "ws-b"])
    assert again == []
    body = (await client.get("/api/workspaces", headers=AUTH)).json()
    assert {r["id"] for r in body} >= {"ws-a", "ws-b"}


async def test_backfill_skips_invalid_slug(
    client: httpx.AsyncClient,
) -> None:
    db = app.state.db
    inserted = await repo.backfill_from_env(
        db, slugs=["valid-slug", "Bad Slug", "another-valid"]
    )
    assert sorted(inserted) == ["another-valid", "valid-slug"]


# --- sandbox / disk / git aggregation --------------------------------------


async def test_list_reports_running_sandbox_with_disk_and_git(
    client: httpx.AsyncClient, install_sandbox,
) -> None:
    mgr, backend = install_sandbox()
    await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-active", "display_name": "Active"},
    )
    # Spin up the sandbox by hitting an endpoint that calls `get_workspace`.
    handle = await mgr.get_workspace("ws-active")
    assert handle is not None

    # Probe order: du -sb /workspace, then git rev-parse --is-inside-work-tree,
    # then git rev-parse --abbrev-ref HEAD, then git status --porcelain=v1.
    # The aggregate caller drains each in sequence; script in the same order.
    backend.script_exec(
        [ExecOutput(stream="stdout", data=b"4194304\t/workspace\n"), ExecExit(exit_code=0)]
    )
    backend.script_exec([ExecExit(exit_code=0)])  # is_inside_work_tree → yes
    backend.script_exec(
        [ExecOutput(stream="stdout", data=b"main\n"), ExecExit(exit_code=0)]
    )
    backend.script_exec(
        [
            ExecOutput(stream="stdout", data=b" M README.md\n"),
            ExecExit(exit_code=0),
        ]
    )

    body = (await client.get("/api/workspaces", headers=AUTH)).json()
    by_id = {r["id"]: r for r in body}
    row = by_id["ws-active"]
    assert row["sandbox"]["state"] == "running"
    assert row["disk"]["used_mb"] == 4  # 4 MiB
    assert row["git"] == {"is_repo": True, "branch": "main", "dirty": True}


async def test_list_reports_absent_when_no_handle_cached(
    client: httpx.AsyncClient, install_sandbox,
) -> None:
    install_sandbox()  # install manager but never spin a workspace sandbox
    await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-cold", "display_name": "Cold"},
    )
    body = (await client.get("/api/workspaces", headers=AUTH)).json()
    by_id = {r["id"]: r for r in body}
    row = by_id["ws-cold"]
    assert row["sandbox"] == {"state": "absent", "exit_code": None}
    assert row["disk"] == {"used_mb": None, "quota_mb": None}
    assert row["git"] == {"is_repo": False, "branch": None, "dirty": False}


async def test_disk_endpoint_runs_du(
    client: httpx.AsyncClient, install_sandbox,
) -> None:
    mgr, backend = install_sandbox()
    await client.post(
        "/api/workspaces",
        headers=AUTH,
        json={"id": "ws-du", "display_name": "Du Test"},
    )
    await mgr.get_workspace("ws-du")
    # First exec the route runs is `du -sb /workspace`.
    backend.script_exec(
        [
            ExecOutput(stream="stdout", data=b"2097152\t/workspace\n"),
            ExecExit(exit_code=0),
        ]
    )
    response = await client.get(
        "/api/workspaces/ws-du/disk", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"used_mb": 2, "quota_mb": None}
