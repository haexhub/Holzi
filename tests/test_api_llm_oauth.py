"""Integration tests for the /api/llm/credentials/oauth/* endpoints.

A real `claude` binary never runs — we swap `app.state.oauth_driver` with
a `ClaudeOAuthDriver` whose `spawn_fn` is a `_FakeClaudeSession`. That
session mirrors what the real CLI would do: emits the URL on stdout when
asked, writes `.credentials.json` into the home dir when authorization
completes, and exits with the right code.
"""
import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hermes.crypto import EncryptedBlob
from hermes.main import app
from hermes.oauth import ClaudeOAuthDriver
from hermes.repository import llm_credentials as repo

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}
FAKE_URL = (
    "https://claude.com/cai/oauth/authorize"
    "?code=true&client_id=abc&response_type=code&state=xyz"
)
FAKE_CREDS = {
    "claudeAiOauth": {
        "accessToken": "fake-token-xyz",
        "refreshToken": "rt",
        "expiresAt": 1_700_000_000_000,
    }
}


class _FakeStdin:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False
        self._exit = asyncio.Event()

    async def wait(self) -> int:
        await self._exit.wait()
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exit.set()


class _FakeClaudeSession:
    """One-shot stand-in for the claude CLI subprocess. Spawning more
    than once is unsupported — the routes only need one session per
    test, and reusing a session would muddy the assertions."""

    def __init__(
        self,
        *,
        credentials_payload: dict | str = FAKE_CREDS,
        emit_url: bool = True,
    ) -> None:
        self.proc: _FakeProcess | None = None
        self.home: str | None = None
        self.credentials_payload = credentials_payload
        self.emit_url = emit_url

    async def spawn(self, cmd: list[str], env: dict[str, str]) -> _FakeProcess:
        assert self.proc is None, "FakeClaudeSession spawned twice"
        self.home = env["HOME"]
        self.proc = _FakeProcess()
        if self.emit_url:
            self.proc.stdout.feed_data(
                f"If the browser didn't open, visit: {FAKE_URL}\n".encode()
            )
        return self.proc

    def complete_authorization(self) -> None:
        assert self.proc is not None and self.home is not None
        target = Path(self.home) / ".claude" / ".credentials.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            self.credentials_payload
            if isinstance(self.credentials_payload, str)
            else json.dumps(self.credentials_payload)
        )
        target.write_text(payload)
        self.proc.returncode = 0
        self.proc.stdout.feed_eof()
        self.proc.stderr.feed_eof()
        self.proc._exit.set()

    def fail_authorization(self, msg: str = "invalid code") -> None:
        assert self.proc is not None
        self.proc.stderr.feed_data((msg + "\n").encode())
        self.proc.returncode = 1
        self.proc.stdout.feed_eof()
        self.proc.stderr.feed_eof()
        self.proc._exit.set()


@pytest.fixture
async def client(pg_db):
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
    ):
        yield c


def _install_driver(session: _FakeClaudeSession) -> ClaudeOAuthDriver:
    driver = ClaudeOAuthDriver(spawn_fn=session.spawn, flow_timeout_s=5.0)
    app.state.oauth_driver = driver
    return driver


@pytest.fixture
def _cleanup_tmp_homes():
    yield
    # Wipe /tmp/hermes-oauth between tests so a leaked flow doesn't poison
    # the next one.
    base = Path("/tmp") / "hermes-oauth"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)


pytestmark = pytest.mark.usefixtures("_cleanup_tmp_homes")


# ─── /oauth/start ──────────────────────────────────────────────────────


async def test_oauth_start_returns_id_and_url_and_persists_pending(
    client: httpx.AsyncClient,
) -> None:
    session = _FakeClaudeSession()
    _install_driver(session)
    r = await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == FAKE_URL
    assert isinstance(body["id"], int) and body["id"] > 0
    row = await repo.get(app.state.db, body["id"])
    assert row is not None
    assert row.mode == "oauth_claude"
    assert row.oauth_status == "pending"
    assert row.provider == "anthropic"


async def test_oauth_start_requires_auth(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/llm/credentials/oauth/start")
    assert r.status_code == 401


async def test_oauth_start_replaces_existing_pending_row(
    client: httpx.AsyncClient,
) -> None:
    session1 = _FakeClaudeSession()
    _install_driver(session1)
    first = (
        await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    ).json()

    # Swap in a fresh driver+session — the previous flow's subprocess is
    # still parked in the driver, but `start` is supposed to tear it down.
    session2 = _FakeClaudeSession()
    _install_driver(session2)
    second = (
        await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    ).json()

    # The old pending row was swept; only the new pending row should be
    # in the DB. SQLite reuses freed ROWIDs by default, so the two ids
    # may legitimately coincide — but there can only be one row.
    rows = await repo.list_all(app.state.db)
    oauth_rows = [r for r in rows if r.mode == "oauth_claude"]
    assert len(oauth_rows) == 1
    assert oauth_rows[0].id == second["id"]
    # The two starts are still distinct events.
    assert first["url"] == FAKE_URL and second["url"] == FAKE_URL


async def test_oauth_start_rolls_back_row_on_spawn_failure(
    client: httpx.AsyncClient,
) -> None:
    # `_URL_WAIT_TIMEOUT_S` is module-level so we can't cheaply force a
    # URL-wait timeout; make the spawn function itself raise so the
    # route falls into the cleanup branch immediately.
    async def boom(cmd, env):
        raise RuntimeError("spawn failed")

    driver = ClaudeOAuthDriver(spawn_fn=boom)
    app.state.oauth_driver = driver

    r = await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    assert r.status_code == 500
    body = r.json()
    assert body["detail"]["code"] == "LLM_OAUTH_START_FAILED"
    assert "spawn failed" in body["detail"]["params"]["message"]
    # The placeholder row should have been deleted.
    rows = await repo.list_all(app.state.db)
    assert [r for r in rows if r.mode == "oauth_claude"] == []


# ─── /oauth/{id}/code ─────────────────────────────────────────────────


async def test_oauth_code_persists_authorized_credential(
    client: httpx.AsyncClient,
) -> None:
    session = _FakeClaudeSession()
    _install_driver(session)
    start = (
        await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    ).json()
    flow_id = start["id"]

    async def complete_after_a_tick() -> None:
        # Wait for the route handler to call submit_code (which writes
        # stdin and awaits exit) before we signal completion.
        await asyncio.sleep(0.02)
        session.complete_authorization()

    completion = asyncio.create_task(complete_after_a_tick())
    r = await client.post(
        f"/api/llm/credentials/oauth/{flow_id}/code",
        json={"code": "abc-123"},
        headers=AUTH,
    )
    await completion
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == flow_id
    assert body["oauth_status"] == "authorized"
    assert body["oauth_authorized_at"] is not None

    # Ciphertext columns must be set; round-trip via the encryptor.
    row = await repo.get(app.state.db, flow_id)
    assert row is not None
    assert row.oauth_status == "authorized"
    assert row.oauth_iv and row.oauth_tag and row.oauth_data
    plaintext = app.state.encryptor.decrypt(
        EncryptedBlob(iv=row.oauth_iv, tag=row.oauth_tag, data=row.oauth_data)
    )
    assert json.loads(plaintext)["claudeAiOauth"]["accessToken"] == "fake-token-xyz"

    # stdin received the code, newline-terminated.
    assert session.proc is not None
    assert session.proc.stdin.chunks == [b"abc-123\n"]

    # The temp HOME should have been wiped.
    home_dir = Path("/tmp") / "hermes-oauth" / str(flow_id)
    assert not home_dir.exists()


async def test_oauth_code_rejects_bad_code(client: httpx.AsyncClient) -> None:
    session = _FakeClaudeSession()
    _install_driver(session)
    flow_id = (
        await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    ).json()["id"]

    async def fail_after_a_tick() -> None:
        await asyncio.sleep(0.02)
        session.fail_authorization("verification code invalid")

    fail = asyncio.create_task(fail_after_a_tick())
    r = await client.post(
        f"/api/llm/credentials/oauth/{flow_id}/code",
        json={"code": "wrong"},
        headers=AUTH,
    )
    await fail
    # 400-class — caller can retry with a fresh /oauth/start.
    assert r.status_code in (400, 422), r.text
    if r.status_code == 400:
        body = r.json()
        assert body["detail"]["code"] == "LLM_OAUTH_CODE_REJECTED"
        assert "verification code invalid" in body["detail"]["params"]["message"]


async def test_oauth_code_unknown_id_returns_404(client: httpx.AsyncClient) -> None:
    _install_driver(_FakeClaudeSession())
    r = await client.post(
        "/api/llm/credentials/oauth/9999/code",
        json={"code": "x"},
        headers=AUTH,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "LLM_OAUTH_FLOW_NOT_FOUND"


# ─── /oauth/{id}/status ───────────────────────────────────────────────


async def test_oauth_status_returns_pending_then_authorized(
    client: httpx.AsyncClient,
) -> None:
    session = _FakeClaudeSession()
    _install_driver(session)
    flow_id = (
        await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    ).json()["id"]

    r1 = await client.get(
        f"/api/llm/credentials/oauth/{flow_id}/status", headers=AUTH
    )
    assert r1.status_code == 200
    assert r1.json() == {"id": flow_id, "status": "pending"}

    # Drive the flow to completion.
    async def go() -> None:
        await asyncio.sleep(0.02)
        session.complete_authorization()

    go_t = asyncio.create_task(go())
    await client.post(
        f"/api/llm/credentials/oauth/{flow_id}/code",
        json={"code": "ok"},
        headers=AUTH,
    )
    await go_t

    r2 = await client.get(
        f"/api/llm/credentials/oauth/{flow_id}/status", headers=AUTH
    )
    assert r2.status_code == 200
    assert r2.json() == {"id": flow_id, "status": "authorized"}


async def test_oauth_status_unknown_id_returns_404(client: httpx.AsyncClient) -> None:
    _install_driver(_FakeClaudeSession())
    r = await client.get("/api/llm/credentials/oauth/12345/status", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["detail"] == "LLM_OAUTH_FLOW_NOT_FOUND"


# ─── /oauth/{id}/cancel ────────────────────────────────────────────────


async def test_oauth_cancel_kills_subprocess_and_deletes_row(
    client: httpx.AsyncClient,
) -> None:
    session = _FakeClaudeSession()
    _install_driver(session)
    flow_id = (
        await client.post("/api/llm/credentials/oauth/start", headers=AUTH)
    ).json()["id"]

    r = await client.post(
        f"/api/llm/credentials/oauth/{flow_id}/cancel", headers=AUTH
    )
    assert r.status_code == 204
    assert session.proc is not None and session.proc.killed

    # Row gone; temp HOME gone.
    assert await repo.get(app.state.db, flow_id) is None
    assert not (Path("/tmp") / "hermes-oauth" / str(flow_id)).exists()


async def test_oauth_cancel_unknown_id_returns_404(
    client: httpx.AsyncClient,
) -> None:
    _install_driver(_FakeClaudeSession())
    r = await client.post(
        "/api/llm/credentials/oauth/9999/cancel", headers=AUTH
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "LLM_OAUTH_FLOW_NOT_FOUND"
