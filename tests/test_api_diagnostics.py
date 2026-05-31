"""End-to-end tests for `GET /api/diagnostics` (Plan 20).

The diagnostics endpoint is the data source for the Control Center's
Diagnostics page. It returns a redacted snapshot of subsystem state so
a new user can see what is missing before first chat — without ever
returning secrets (API keys, ciphertext, master key material).

Each check carries `status` (ok | warning | error) and a short human
message; the overall status is the worst of the children. Tests fix
the contract (shape, redaction, status math) so the frontend can rely
on it.
"""
import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.main import app
from hermes.repository import llm_credentials as llm_repo
from hermes.repository import messenger as messenger_repo
from hermes.repository import workspaces as workspaces_repo

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}

CHECK_IDS = {"database", "llm", "messenger", "scheduler", "workspace", "sandbox"}


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


# ---------------------------------------------------------------------------
# auth + shape
# ---------------------------------------------------------------------------


async def test_diagnostics_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/diagnostics")
    assert response.status_code == 401


async def test_diagnostics_returns_all_checks(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/diagnostics", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert "checks" in body
    assert "overall" in body
    assert body["overall"] in {"ok", "warning", "error"}
    ids = {c["id"] for c in body["checks"]}
    assert ids == CHECK_IDS
    for check in body["checks"]:
        assert set(check.keys()) >= {"id", "label", "status", "message"}
        assert check["status"] in {"ok", "warning", "error"}
        assert isinstance(check["message"], str)


# ---------------------------------------------------------------------------
# default (fresh) state
# ---------------------------------------------------------------------------


def _check(body: dict, check_id: str) -> dict:
    for c in body["checks"]:
        if c["id"] == check_id:
            return c
    raise AssertionError(f"check {check_id!r} not in response")


async def test_diagnostics_default_state_flags_missing_setup(
    client: httpx.AsyncClient,
) -> None:
    """Fresh boot: no LLM credential, no workspace roots, no sandbox socket
    → warnings, but db + scheduler are ok. Messenger is an optional bridge,
    so its absence stays `ok` and doesn't pollute the overall badge."""
    body = (await client.get("/api/diagnostics", headers=AUTH)).json()

    assert _check(body, "database")["status"] == "ok"
    assert _check(body, "scheduler")["status"] == "ok"
    assert _check(body, "llm")["status"] == "warning"
    assert _check(body, "messenger")["status"] == "ok"
    assert _check(body, "workspace")["status"] == "warning"
    assert _check(body, "sandbox")["status"] == "warning"
    # Overall = worst of children → warning when anything is < ok.
    assert body["overall"] == "warning"


# ---------------------------------------------------------------------------
# llm credential redaction
# ---------------------------------------------------------------------------


async def test_diagnostics_with_active_llm_credential_does_not_leak_secrets(
    client: httpx.AsyncClient,
) -> None:
    plaintext = "sk-extremely-secret-key-do-not-leak"
    blob = app.state.encryptor.encrypt(plaintext)
    cred = await llm_repo.create_api_key(
        app.state.db,
        provider="openai",
        display_name="Martin OpenAI",
        base_url=None,
        ciphertext=blob,
    )
    await llm_repo.activate(app.state.db, cred.id)

    response = await client.get("/api/diagnostics", headers=AUTH)
    body = response.json()
    llm = _check(body, "llm")

    assert llm["status"] == "ok"
    # The display name and provider are public-ish identification — fine.
    assert "openai" in llm["message"].lower() or "openai" in str(llm).lower()
    # The plaintext key and the ciphertext blob must NEVER appear anywhere
    # in the response.
    raw = response.text
    assert plaintext not in raw
    # EncryptedBlob fields are hex strings (see hermes.crypto.Encryptor.encrypt).
    assert blob.data not in raw
    assert blob.iv not in raw
    assert blob.tag not in raw


# ---------------------------------------------------------------------------
# messenger
# ---------------------------------------------------------------------------


async def test_diagnostics_with_active_messenger_account_reports_ok(
    client: httpx.AsyncClient,
) -> None:
    phone = "+491701234567"
    account = await messenger_repo.create_signal(app.state.db, phone)
    await messenger_repo.activate(app.state.db, account.id)

    response = await client.get("/api/diagnostics", headers=AUTH)
    body = response.json()
    messenger = _check(body, "messenger")

    assert messenger["status"] == "ok"
    # Distinguishes the "configured" case from the "optional / not set up" case.
    assert "active account" in messenger["message"]
    # Phone numbers are PII — must not be returned verbatim.
    assert phone not in response.text


async def test_diagnostics_without_messenger_account_is_ok_not_warning(
    client: httpx.AsyncClient,
) -> None:
    """Messenger is an optional Signal/Telegram bridge — its absence is a
    valid web-only configuration, not a setup issue. Status must be `ok` so
    the overall badge stays green when only LLM is configured."""
    response = await client.get("/api/diagnostics", headers=AUTH)
    messenger = _check(response.json(), "messenger")

    assert messenger["status"] == "ok"
    assert "optional" in messenger["message"].lower()


# ---------------------------------------------------------------------------
# workspaces (Plan 25-A: DB-driven; the env is bootstrap-only)
# ---------------------------------------------------------------------------


async def test_diagnostics_with_workspaces_in_db_reports_ok(
    client: httpx.AsyncClient,
) -> None:
    """Plan 25-A: the `workspaces` table is the source of truth. Two active
    rows → status ok, display_name surfaces in the message."""
    await workspaces_repo.create(
        app.state.db, workspace_id="holzi", display_name="Holzi"
    )
    await workspaces_repo.create(
        app.state.db, workspace_id="hermes", display_name="Hermes"
    )
    response = await client.get("/api/diagnostics", headers=AUTH)
    workspace = _check(response.json(), "workspace")
    assert workspace["status"] == "ok"
    assert "Holzi" in workspace["message"] or "2" in workspace["message"]


async def test_diagnostics_with_env_set_but_empty_table_still_warns(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards against regressions: even when `HERMES_WORKSPACE_ROOTS` is set
    at request time, the check must ignore it and look at the table only.
    (The lifespan backfill normally inserts env slugs at boot, but if a
    test bypasses that flow the env alone must not count as configured.)"""
    from hermes import config as hermes_config

    monkeypatch.setattr(
        hermes_config.settings, "workspace_roots", "from-env-only"
    )
    response = await client.get("/api/diagnostics", headers=AUTH)
    workspace = _check(response.json(), "workspace")
    assert workspace["status"] == "warning"
    # New copy points at the UI surface, not the env.
    assert "/settings/workspaces" in workspace["message"]
    assert "HERMES_WORKSPACE_ROOTS" not in workspace["message"]


async def test_diagnostics_excludes_archived_workspaces(
    client: httpx.AsyncClient,
) -> None:
    """`list_active` excludes archived rows. A workspace that's been
    archived via the UI must not keep the check green on its own."""
    await workspaces_repo.create(
        app.state.db, workspace_id="ghost", display_name="Ghost"
    )
    await workspaces_repo.archive(app.state.db, "ghost")
    response = await client.get("/api/diagnostics", headers=AUTH)
    workspace = _check(response.json(), "workspace")
    assert workspace["status"] == "warning"


# ---------------------------------------------------------------------------
# liveness edges
# ---------------------------------------------------------------------------


async def test_diagnostics_reports_error_when_scheduler_task_died(
    client: httpx.AsyncClient,
) -> None:
    """The scheduler manager survives a crashed background loop —
    `is not None` would silently report 'ok'. Killing the task should
    flip the check to error."""
    scheduler = app.state.scheduler
    assert scheduler is not None
    original_task = scheduler._task
    try:
        scheduler._task = None
        response = await client.get("/api/diagnostics", headers=AUTH)
        sched = _check(response.json(), "scheduler")
        assert sched["status"] == "error"
    finally:
        scheduler._task = original_task


async def test_diagnostics_handles_missing_db_engine(
    client: httpx.AsyncClient,
) -> None:
    """If the DB engine isn't on app.state the LLM and messenger checks
    can't run; they must surface as 'error' rather than crashing."""
    original_db = app.state.db
    try:
        app.state.db = None
        response = await client.get("/api/diagnostics", headers=AUTH)
        body = response.json()
        assert _check(body, "database")["status"] == "error"
        assert _check(body, "llm")["status"] == "error"
        assert _check(body, "messenger")["status"] == "error"
        assert body["overall"] == "error"
    finally:
        app.state.db = original_db


# ---------------------------------------------------------------------------
# user-input truncation
# ---------------------------------------------------------------------------


async def test_diagnostics_truncates_long_display_name(
    client: httpx.AsyncClient,
) -> None:
    """`display_name` is user-controlled — an oversized or multiline
    value must not dominate the response."""
    long_name = "x" * 500 + "\nsecond line"
    blob = app.state.encryptor.encrypt("sk-test")
    cred = await llm_repo.create_api_key(
        app.state.db,
        provider="openai",
        display_name=long_name,
        base_url=None,
        ciphertext=blob,
    )
    await llm_repo.activate(app.state.db, cred.id)

    response = await client.get("/api/diagnostics", headers=AUTH)
    msg = _check(response.json(), "llm")["message"]
    # Length cap (48) + the surrounding "active credential: … (model …)"
    # chrome stays under a comfortable ceiling.
    assert len(msg) < 120
    assert "\n" not in msg


async def test_diagnostics_truncates_long_workspaces_list(
    client: httpx.AsyncClient,
) -> None:
    """The workspace message is meant to be short — many configured
    workspaces should collapse to a count + first-few preview using
    `display_name` (sorted ascending)."""
    for i in range(20):
        await workspaces_repo.create(
            app.state.db,
            workspace_id=f"workspace-{i:02d}",
            display_name=f"Workspace {i:02d}",
        )
    response = await client.get("/api/diagnostics", headers=AUTH)
    msg = _check(response.json(), "workspace")["message"]
    assert "20 workspace(s)" in msg
    # First three (display_name ascending) should appear, trailing ones not.
    assert "Workspace 00" in msg
    assert "Workspace 19" not in msg
    assert "…" in msg


async def test_diagnostics_truncates_long_workspace_display_name(
    client: httpx.AsyncClient,
) -> None:
    """`display_name` is user-controlled — an oversized or multiline value
    must not dominate the response (same defence-in-depth as the LLM check)."""
    long_name = "x" * 500 + "\nsecond line"
    await workspaces_repo.create(
        app.state.db, workspace_id="big", display_name=long_name
    )
    response = await client.get("/api/diagnostics", headers=AUTH)
    msg = _check(response.json(), "workspace")["message"]
    assert len(msg) < 200
    assert "\n" not in msg
