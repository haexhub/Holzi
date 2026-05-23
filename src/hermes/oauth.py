"""Drives the `claude auth login --claudeai` subprocess for a single
OAuth flow.

Real flow (claude-code 2.1.x):
  1. spawn("claude", ["auth", "login", "--claudeai"], env={HOME})
  2. CLI prints:
        Opening browser to sign in…
        If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?…
        Paste code here if prompted >  ← reads from stdin
  3. user authorizes via the URL; claude.com shows a verification code
  4. user submits the code through our UI; we forward `<code>\n` to the
     held-open subprocess's stdin
  5. CLI verifies via PKCE, writes $HOME/.claude/.credentials.json, exits 0

Each driver instance keeps an in-memory map keyed on the `llm_credentials`
row id so the status / submit-code / cancel endpoints can find their
subprocess later. Callers MUST `cancel` abandoned flows — there's a
15-minute backstop but a leaked subprocess is still bad form.

The subprocess factory is injectable for tests so we never spawn a real
`claude` binary in CI.
"""
import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_FLOW_TIMEOUT_S = 15 * 60
DEFAULT_URL_RE = re.compile(
    r"https://claude\.[a-z]+/cai/oauth/authorize\?\S+",
    re.IGNORECASE,
)
_URL_WAIT_TIMEOUT_S = 30.0
_URL_POLL_INTERVAL_S = 0.025


class OAuthDriverError(RuntimeError):
    """Anything the OAuth subprocess flow can fail with — raised by the
    driver, mapped to HTTP errors by the route layer."""


class _StdinLike(Protocol):
    def write(self, data: bytes) -> Any: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


class _ReaderLike(Protocol):
    async def read(self, n: int = -1) -> bytes: ...


class _ProcessLike(Protocol):
    @property
    def stdin(self) -> _StdinLike | None: ...
    @property
    def stdout(self) -> _ReaderLike | None: ...
    @property
    def stderr(self) -> _ReaderLike | None: ...
    @property
    def returncode(self) -> int | None: ...
    async def wait(self) -> int: ...
    def kill(self) -> None: ...


SpawnFn = Callable[[list[str], dict[str, str]], Awaitable[_ProcessLike]]


async def _default_spawn(
    cmd: list[str], env: dict[str, str]
) -> _ProcessLike:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    return proc  # type: ignore[return-value]


@dataclass
class _PendingFlow:
    flow_id: int
    proc: _ProcessLike
    home: str
    stdout_buf: bytearray = field(default_factory=bytearray)
    stderr_buf: bytearray = field(default_factory=bytearray)
    code_submitted: bool = False
    url: str = ""
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    wait_task: asyncio.Task[None] | None = None
    timeout_task: asyncio.Task[None] | None = None
    # Set in `start_login` once we have a running loop — can't build a
    # Future at module import time.
    done: asyncio.Future[None] | None = None


class ClaudeOAuthDriver:
    def __init__(
        self,
        *,
        claude_bin: str | None = None,
        spawn_fn: SpawnFn | None = None,
        flow_timeout_s: float = DEFAULT_FLOW_TIMEOUT_S,
        url_regex: re.Pattern[str] = DEFAULT_URL_RE,
    ) -> None:
        self.claude_bin = claude_bin or os.environ.get("CLAUDE_BIN", "claude")
        self.spawn_fn: SpawnFn = spawn_fn or _default_spawn
        self.flow_timeout_s = flow_timeout_s
        self.url_regex = url_regex
        self._flows: dict[int, _PendingFlow] = {}

    async def start_login(self, *, flow_id: int, home: str) -> str:
        if flow_id in self._flows:
            raise OAuthDriverError(f"flow already active for id {flow_id}")

        Path(home, ".claude").mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["HOME"] = home
        # Strip CLAUDECODE so the CLI doesn't refuse "cannot launch
        # inside another Claude Code session" when Hermes itself runs in
        # a Claude-Code dev shell.
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        proc = await self.spawn_fn(
            [self.claude_bin, "auth", "login", "--claudeai"], env
        )

        loop = asyncio.get_running_loop()
        flow = _PendingFlow(
            flow_id=flow_id,
            proc=proc,
            home=home,
        )
        flow.done = loop.create_future()
        # Avoid "Future exception was never retrieved" noise if cancel
        # or timeout fires before any caller awaits flow.done.
        flow.done.add_done_callback(
            lambda f: None if f.cancelled() else f.exception()
        )
        self._flows[flow_id] = flow

        flow.stdout_task = asyncio.create_task(
            self._drain(proc.stdout, flow.stdout_buf)
        )
        flow.stderr_task = asyncio.create_task(
            self._drain(proc.stderr, flow.stderr_buf)
        )
        flow.wait_task = asyncio.create_task(self._await_exit(flow))
        flow.timeout_task = asyncio.create_task(self._timeout_backstop(flow))

        try:
            url = await self._wait_for_url(flow, _URL_WAIT_TIMEOUT_S)
        except Exception:
            await self.cancel(flow_id)
            raise
        flow.url = url
        return url

    async def submit_code(self, flow_id: int, code: str) -> None:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise OAuthDriverError(
                f"no active flow with id {flow_id} "
                "(timed out, completed, or never started)"
            )
        if flow.code_submitted:
            raise OAuthDriverError("code already submitted for this flow")
        if flow.proc.stdin is None or flow.done is None:
            raise OAuthDriverError("subprocess stdin is not available")
        flow.code_submitted = True
        try:
            flow.proc.stdin.write(f"{code.strip()}\n".encode())
            await flow.proc.stdin.drain()
            flow.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            # CLI may have already exited (bad code path). The error will
            # surface via flow.done below.
            pass
        await flow.done

    async def cancel(self, flow_id: int) -> None:
        flow = self._flows.pop(flow_id, None)
        if flow is None:
            return
        if flow.timeout_task is not None:
            flow.timeout_task.cancel()
        try:
            if flow.proc.returncode is None:
                flow.proc.kill()
        except (ProcessLookupError, OSError):
            pass
        if flow.done is not None and not flow.done.done():
            flow.done.set_exception(OAuthDriverError("flow cancelled"))
        # Best-effort cleanup; reader tasks finish when the pipes EOF, and
        # wait_task finishes when the killed proc reaps. We don't await
        # here to keep cancel() snappy.

    def active_ids(self) -> list[int]:
        return list(self._flows.keys())

    async def _drain(
        self, reader: _ReaderLike | None, buf: bytearray
    ) -> None:
        if reader is None:
            return
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            buf.extend(chunk)

    async def _await_exit(self, flow: _PendingFlow) -> None:
        code = await flow.proc.wait()
        # Flush remaining buffered output before deciding success/failure.
        for task in (flow.stdout_task, flow.stderr_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if flow.done is None or flow.done.done():
            self._flows.pop(flow.flow_id, None)
            return
        if code == 0:
            flow.done.set_result(None)
        else:
            stderr = (
                bytes(flow.stderr_buf).decode("utf-8", errors="replace").strip()
            )
            stdout = (
                bytes(flow.stdout_buf).decode("utf-8", errors="replace").strip()
            )
            msg = stderr or stdout or "no output"
            flow.done.set_exception(
                OAuthDriverError(f"claude auth login exited {code}: {msg}")
            )
        self._flows.pop(flow.flow_id, None)

    async def _timeout_backstop(self, flow: _PendingFlow) -> None:
        try:
            await asyncio.sleep(self.flow_timeout_s)
        except asyncio.CancelledError:
            return
        if flow.flow_id in self._flows:
            await self.cancel(flow.flow_id)

    async def _wait_for_url(
        self, flow: _PendingFlow, timeout_s: float
    ) -> str:
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < timeout_s:
            m = self.url_regex.search(
                bytes(flow.stdout_buf).decode("utf-8", errors="replace")
            )
            if m:
                return m.group(0)
            if flow.flow_id not in self._flows:
                stderr = (
                    bytes(flow.stderr_buf)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                raise OAuthDriverError(
                    "claude auth login failed before printing URL: "
                    + (stderr or "no stderr")
                )
            # Bail early if the process already exited without the URL.
            if flow.proc.returncode is not None:
                stderr = (
                    bytes(flow.stderr_buf)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                raise OAuthDriverError(
                    f"claude auth login exited {flow.proc.returncode} "
                    f"before printing URL: {stderr or 'no stderr'}"
                )
            await asyncio.sleep(_URL_POLL_INTERVAL_S)
        raise OAuthDriverError(
            f"claude auth login did not print a URL within {timeout_s}s"
        )


# ─── credentials.json parsing ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CredentialsFile:
    """Raw `.credentials.json` content + parsed expiry (when available).

    `raw` is the full file contents; we store the whole blob in DB so the
    proxy resolver can hand it back to the CLI on the next spawn. `expires_at_ms`
    is best-effort — if we can't find an `expiresAt` anywhere in the
    JSON, the row stays usable but the UI loses the "expires on X" hint.
    """

    raw: str
    expires_at_ms: int | None


async def read_credentials_raw_and_expiry(home: str) -> CredentialsFile | None:
    """Read `<home>/.claude/.credentials.json` and parse its expiry.

    Returns None if the file is absent or empty (caller maps to "OAuth
    login didn't actually complete"). Malformed JSON still returns a
    `CredentialsFile` with `expires_at_ms=None` — we'd rather persist a
    file we can't parse than refuse the whole flow over a CLI quirk.
    """
    path = Path(home) / ".claude" / ".credentials.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if not raw.strip():
        return None
    expires_ms = _extract_expires_ms(raw)
    return CredentialsFile(raw=raw, expires_at_ms=expires_ms)


def _extract_expires_ms(raw: str) -> int | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # Walk parsed and one level of nested objects looking for an
    # `expiresAt` (numeric ms) or `expires_at` (ISO string). The exact
    # nesting differs between claude-code versions.
    candidates: list[dict[str, Any]] = [parsed]
    for v in parsed.values():
        if isinstance(v, dict):
            candidates.append(v)
    for obj in candidates:
        ms = obj.get("expiresAt")
        if isinstance(ms, int):
            return ms
        iso = obj.get("expires_at")
        if isinstance(iso, str):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return None


# ─── ephemeral $HOME helpers ──────────────────────────────────────────


def oauth_temp_home(flow_id: int) -> str:
    """Per-flow ephemeral HOME for the claude CLI.

    `/tmp/hermes-oauth/<flow_id>` — wiped by `remove_oauth_temp_home`
    after the code endpoint reads `.credentials.json`. Sharing a parent
    dir means tests can blow away everything via `shutil.rmtree` if a
    flow leaks.
    """
    base = Path(tempfile.gettempdir()) / "hermes-oauth" / str(flow_id)
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def remove_oauth_temp_home(flow_id: int) -> None:
    base = Path(tempfile.gettempdir()) / "hermes-oauth" / str(flow_id)
    shutil.rmtree(base, ignore_errors=True)
