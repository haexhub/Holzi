"""Unit tests for the Claude OAuth subprocess driver.

The driver never spawns a real `claude` binary in tests — we inject a
fake spawn function that returns a hand-controlled process with feedable
stdout/stderr StreamReaders and a capturable stdin.

Real CLI shape (claude-code 2.1.x):
    Opening browser to sign in…
    If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?…
    Paste code here if prompted >
"""
import asyncio
import json
from pathlib import Path

import pytest

from hermes.oauth import (
    ClaudeOAuthDriver,
    OAuthDriverError,
    read_credentials_raw_and_expiry,
)

FAKE_URL = (
    "https://claude.com/cai/oauth/authorize"
    "?code=true&client_id=abc&response_type=code&state=xyz"
)


class _FakeStdin:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    """Minimal asyncio.subprocess.Process stand-in. Tests drive it by
    calling `emit_stdout`, `emit_stderr`, and `exit`."""

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

    # ── test helpers ────────────────────────────────────────────────
    def emit_stdout(self, s: str) -> None:
        self.stdout.feed_data(s.encode("utf-8"))

    def emit_stderr(self, s: str) -> None:
        self.stderr.feed_data(s.encode("utf-8"))

    def exit(self, code: int) -> None:
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exit.set()


def _fake_spawn_factory(child: _FakeProcess):
    async def fake_spawn(cmd: list[str], env: dict[str, str]) -> _FakeProcess:
        return child

    return fake_spawn


async def _emit_after(delay: float, fn) -> None:
    await asyncio.sleep(delay)
    fn()


async def test_start_login_parses_url_from_stdout(tmp_path: Path) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    home = str(tmp_path / "home")

    async def emit_url() -> None:
        await asyncio.sleep(0.01)
        child.emit_stdout(
            "Opening browser to sign in…\n"
            f"If the browser didn't open, visit: {FAKE_URL}\n"
            "Paste code here if prompted > "
        )

    asyncio.create_task(emit_url())
    url = await driver.start_login(flow_id=1, home=home)
    assert url == FAKE_URL
    # Driver pre-creates $HOME/.claude/ so the CLI has somewhere to write.
    assert (Path(home) / ".claude").is_dir()
    await driver.cancel(1)


async def test_start_login_rejects_when_cli_exits_before_url(tmp_path: Path) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))

    async def fail_fast() -> None:
        await asyncio.sleep(0.01)
        child.emit_stderr("login server unreachable\n")
        child.exit(1)

    asyncio.create_task(fail_fast())
    with pytest.raises(OAuthDriverError):
        await driver.start_login(flow_id=2, home=str(tmp_path / "h"))
    assert driver.active_ids() == []


async def test_start_login_rejects_duplicate_id(tmp_path: Path) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    home = str(tmp_path / "h")

    async def emit_url() -> None:
        await asyncio.sleep(0.01)
        child.emit_stdout(f"visit: {FAKE_URL}\n")

    asyncio.create_task(emit_url())
    await driver.start_login(flow_id=3, home=home)
    with pytest.raises(OAuthDriverError, match="already active"):
        await driver.start_login(flow_id=3, home=home)
    await driver.cancel(3)


async def test_submit_code_pipes_stdin_and_resolves_on_clean_exit(
    tmp_path: Path,
) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    home = str(tmp_path / "h")

    async def emit_url() -> None:
        await asyncio.sleep(0.01)
        child.emit_stdout(f"visit: {FAKE_URL}\n")

    asyncio.create_task(emit_url())
    await driver.start_login(flow_id=4, home=home)

    async def succeed() -> None:
        await asyncio.sleep(0.02)
        child.exit(0)

    asyncio.create_task(succeed())
    await driver.submit_code(4, "abc-123-xyz")
    assert child.stdin.chunks == [b"abc-123-xyz\n"]
    assert driver.active_ids() == []


async def test_submit_code_rejects_when_cli_exits_nonzero(tmp_path: Path) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    home = str(tmp_path / "h")

    async def emit_url() -> None:
        await asyncio.sleep(0.01)
        child.emit_stdout(f"visit: {FAKE_URL}\n")

    asyncio.create_task(emit_url())
    await driver.start_login(flow_id=5, home=home)

    async def fail() -> None:
        await asyncio.sleep(0.02)
        child.emit_stderr("invalid code\n")
        child.exit(1)

    asyncio.create_task(fail())
    with pytest.raises(OAuthDriverError):
        await driver.submit_code(5, "wrong-code")


async def test_submit_code_unknown_flow_raises() -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    with pytest.raises(OAuthDriverError, match="no active flow"):
        await driver.submit_code(999, "x")


async def test_submit_code_double_submit_raises(tmp_path: Path) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    home = str(tmp_path / "h")

    async def emit_url() -> None:
        await asyncio.sleep(0.01)
        child.emit_stdout(f"visit: {FAKE_URL}\n")

    asyncio.create_task(emit_url())
    await driver.start_login(flow_id=6, home=home)

    # Kick off the first submit but don't await its completion yet.
    first = asyncio.create_task(driver.submit_code(6, "code-a"))
    # Give the driver a tick to mark code_submitted=True before second call.
    await asyncio.sleep(0.005)
    with pytest.raises(OAuthDriverError, match="already submitted"):
        await driver.submit_code(6, "code-b")
    child.exit(0)
    await first


async def test_cancel_kills_subprocess_and_clears_flow(tmp_path: Path) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    home = str(tmp_path / "h")

    async def emit_url() -> None:
        await asyncio.sleep(0.01)
        child.emit_stdout(f"visit: {FAKE_URL}\n")

    asyncio.create_task(emit_url())
    await driver.start_login(flow_id=7, home=home)
    await driver.cancel(7)
    assert child.killed
    assert driver.active_ids() == []


async def test_cancel_idempotent_for_unknown_id() -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(spawn_fn=_fake_spawn_factory(child))
    await driver.cancel(12345)  # no-op, must not raise


async def test_hard_timeout_kills_subprocess(tmp_path: Path) -> None:
    child = _FakeProcess()
    driver = ClaudeOAuthDriver(
        spawn_fn=_fake_spawn_factory(child), flow_timeout_s=0.05
    )
    home = str(tmp_path / "h")

    async def emit_url() -> None:
        await asyncio.sleep(0.01)
        child.emit_stdout(f"visit: {FAKE_URL}\n")

    asyncio.create_task(emit_url())
    await driver.start_login(flow_id=8, home=home)
    # Wait past the timeout.
    await asyncio.sleep(0.15)
    assert child.killed
    assert driver.active_ids() == []


# ─── read_credentials_raw_and_expiry ────────────────────────────────


async def test_read_credentials_returns_none_when_missing(tmp_path: Path) -> None:
    assert await read_credentials_raw_and_expiry(str(tmp_path)) is None


async def test_read_credentials_handles_top_level_expires_at_ms(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / ".credentials.json"
    target.parent.mkdir(parents=True)
    future_ms = 1_700_000_000_000
    target.write_text(json.dumps({"accessToken": "x", "expiresAt": future_ms}))
    result = await read_credentials_raw_and_expiry(str(tmp_path))
    assert result is not None
    assert result.expires_at_ms == future_ms
    assert json.loads(result.raw)["accessToken"] == "x"


async def test_read_credentials_handles_nested_oauth_shape(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / ".credentials.json"
    target.parent.mkdir(parents=True)
    future_ms = 1_700_000_000_000
    target.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": future_ms}})
    )
    result = await read_credentials_raw_and_expiry(str(tmp_path))
    assert result is not None
    assert result.expires_at_ms == future_ms


async def test_read_credentials_returns_none_expiry_for_malformed_json(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".claude" / ".credentials.json"
    target.parent.mkdir(parents=True)
    target.write_text("not json at all")
    result = await read_credentials_raw_and_expiry(str(tmp_path))
    assert result is not None
    assert result.expires_at_ms is None
    assert result.raw == "not json at all"
