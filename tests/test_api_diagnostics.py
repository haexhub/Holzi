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
    """Fresh boot: no LLM credential, no messenger, no workspace roots,
    no sandbox socket → warnings, but db + scheduler are ok."""
    body = (await client.get("/api/diagnostics", headers=AUTH)).json()

    assert _check(body, "database")["status"] == "ok"
    assert _check(body, "scheduler")["status"] == "ok"
    assert _check(body, "llm")["status"] == "warning"
    assert _check(body, "messenger")["status"] == "warning"
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
    # Phone numbers are PII — must not be returned verbatim.
    assert phone not in response.text


# ---------------------------------------------------------------------------
# workspace roots
# ---------------------------------------------------------------------------


async def test_diagnostics_with_workspace_roots_configured(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes import config as hermes_config

    monkeypatch.setattr(
        hermes_config.settings, "workspace_roots", "holzi,hermes"
    )
    response = await client.get("/api/diagnostics", headers=AUTH)
    workspace = _check(response.json(), "workspace")
    assert workspace["status"] == "ok"
    # The root ids themselves are configured user-facing names, fine to surface.
    assert "holzi" in workspace["message"] or "2" in workspace["message"]


# ---------------------------------------------------------------------------
# auth_token redaction
# ---------------------------------------------------------------------------


async def test_diagnostics_does_not_leak_auth_token(
    client: httpx.AsyncClient,
) -> None:
    """The bearer token reached the endpoint, but the response must never
    echo it back."""
    response = await client.get("/api/diagnostics", headers=AUTH)
    assert VALID_TOKEN not in response.text
