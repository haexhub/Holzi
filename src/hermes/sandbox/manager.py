"""SandboxManager — the single owner of sandbox lifecycle.

Lives on `app.state` (single-worker / single-user invariant, like the existing
`chat_runs` and `approvals` registries). Workspace sandboxes auto-start on
first use and persist; ephemeral sandboxes are created per call and always
removed. The manager is the only place that builds a `SandboxSpec`, so it is
the chokepoint that guarantees every sandbox gets resource limits and lands on
the isolated network — never the agent's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager

import structlog

from hermes.sandbox.backend import SandboxBackend
from hermes.sandbox.errors import SandboxError, SandboxFileTooLarge
from hermes.sandbox.models import (
    FILE_SIZE_CAP,
    ExecEvent,
    ResourceLimits,
    SandboxHandle,
    SandboxKind,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)

# States the health watcher treats as "the workspace is dead, surface it."
# A clean `exited` is excluded — workspaces idle on `sleep infinity`, so a
# zero-exit is unusual but not a crash signal that warrants an alert.
_DEAD_STATES = frozenset({SandboxState.crashed, SandboxState.oom, SandboxState.removed})

CrashHandler = Callable[["WorkspaceCrash"], Awaitable[None]]

logger = structlog.get_logger(__name__)


class WorkspaceCrash:
    """Surface-only signal that a workspace sandbox is dead.

    Emitted by the health watcher exactly once per crash transition. The agent
    converts this into the `sandbox_crashed` SSE event; the user decides whether
    to call `restart_workspace`. The watcher never restarts on its own — that
    keeps crash-loops from amplifying and keeps the audit trail honest."""

    __slots__ = ("workspace_id", "sandbox_id", "state", "exit_code")

    def __init__(
        self,
        *,
        workspace_id: str,
        sandbox_id: str,
        state: SandboxState,
        exit_code: int | None,
    ) -> None:
        self.workspace_id = workspace_id
        self.sandbox_id = sandbox_id
        self.state = state
        self.exit_code = exit_code


class SandboxManager:
    def __init__(
        self,
        *,
        backend: SandboxBackend,
        image: str,
        network: str,
        default_limits: ResourceLimits,
    ) -> None:
        self._backend = backend
        self._image = image
        self._network = network
        self._limits = default_limits
        # workspace_id -> handle of the persistent sandbox for that workspace.
        self._workspaces: dict[str, SandboxHandle] = {}
        # Serialises lazy create/restart so two concurrent awaits for the same
        # workspace can't spawn (and leak) two containers.
        self._lock = asyncio.Lock()
        # Health watcher state: a single background task polls workspace
        # liveness and fires `_crash_handlers` exactly once per dead transition.
        self._crash_handlers: list[CrashHandler] = []
        # workspace_id -> sandbox_id we have already reported as crashed. Used
        # to dedupe so the watcher doesn't spam events on every poll while a
        # workspace stays dead.
        self._reported_crashes: dict[str, str] = {}
        self._watcher_task: asyncio.Task[None] | None = None
        self._watcher_interval: float = 5.0

    # --- spec construction (the isolation/limits chokepoint) ---------------

    def _workspace_spec(self, workspace_id: str) -> SandboxSpec:
        return SandboxSpec(
            kind=SandboxKind.workspace,
            image=self._image,
            network=self._network,
            limits=self._limits,
            workspace_id=workspace_id,
            volume=f"hermes-ws-{workspace_id}",
        )

    def _ephemeral_spec(self) -> SandboxSpec:
        return SandboxSpec(
            kind=SandboxKind.ephemeral,
            image=self._image,
            network=self._network,
            limits=self._limits,
        )

    # --- workspace lifecycle ----------------------------------------------

    def peek_workspace(self, workspace_id: str) -> SandboxHandle | None:
        """Return the cached workspace handle without starting a sandbox.

        Distinct from `get_workspace`: the GET sandbox-status endpoint wants
        "do you have a handle?" answered without side effects, so it can
        report `absent` instead of inadvertently spinning up a container."""
        return self._workspaces.get(workspace_id)

    async def get_workspace(self, workspace_id: str) -> SandboxHandle:
        """Return the workspace sandbox, starting it on first use."""
        existing = self._workspaces.get(workspace_id)
        if existing is not None:
            return existing
        async with self._lock:
            # Re-check under the lock: another awaiter may have created it.
            existing = self._workspaces.get(workspace_id)
            if existing is not None:
                return existing
            handle = await self._backend.create(self._workspace_spec(workspace_id))
            self._workspaces[workspace_id] = handle
            logger.info(
                "sandbox_workspace_started",
                workspace_id=workspace_id,
                sandbox_id=handle.id,
            )
            return handle

    async def restart_workspace(self, workspace_id: str) -> SandboxHandle:
        """Tear down the (possibly dead) workspace sandbox and start a fresh one."""
        async with self._lock:
            existing = self._workspaces.pop(workspace_id, None)
            if existing is not None:
                await self._safe_remove(existing)
            # Clear the dedupe entry so a fresh crash on the new container
            # fires another event instead of being silently swallowed.
            self._reported_crashes.pop(workspace_id, None)
        logger.info("sandbox_workspace_restarting", workspace_id=workspace_id)
        return await self.get_workspace(workspace_id)

    # --- ephemeral lifecycle ----------------------------------------------

    @asynccontextmanager
    async def ephemeral(self) -> AsyncIterator[SandboxHandle]:
        """One-shot sandbox, always removed on exit (success or exception)."""
        handle = await self._backend.create(self._ephemeral_spec())
        try:
            yield handle
        finally:
            await self._safe_remove(handle)

    # --- exec / status -----------------------------------------------------

    async def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> AsyncIterator[ExecEvent]:
        async for event in self._backend.exec(handle, argv, cwd=cwd, env=env):
            yield event

    async def read_file(self, handle: SandboxHandle, path: str) -> bytes:
        return await self._backend.read_file(handle, path)

    async def write_file(
        self, handle: SandboxHandle, path: str, data: bytes
    ) -> None:
        # Reject before the bytes hit the wire so a runaway caller can't ship
        # a multi-GB payload into the runtime and then have the backend bail
        # post-allocation. Backends enforce again as defence in depth.
        if len(data) > FILE_SIZE_CAP:
            raise SandboxFileTooLarge(
                f"write to {path} is {len(data)} bytes, cap is {FILE_SIZE_CAP}"
            )
        await self._backend.write_file(handle, path, data)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        return await self._backend.status(handle)

    # --- health watcher ----------------------------------------------------

    def add_crash_handler(self, handler: CrashHandler) -> None:
        """Subscribe to workspace-crash events. Handlers run sequentially in
        the watcher's task and must be fast / non-blocking — a slow handler
        delays every other workspace's poll *and* `stop_health_watcher`.
        Registering the same handler twice is a no-op."""
        if handler not in self._crash_handlers:
            self._crash_handlers.append(handler)

    def remove_crash_handler(self, handler: CrashHandler) -> None:
        """Unsubscribe a handler. No-op if it isn't registered — callers in
        SSE/finally paths can call this without first checking."""
        try:
            self._crash_handlers.remove(handler)
        except ValueError:
            pass

    async def start_health_watcher(self, *, interval: float = 5.0) -> None:
        """Begin polling cached workspaces for liveness. Idempotent."""
        if self._watcher_task is not None and not self._watcher_task.done():
            return
        self._watcher_interval = interval
        self._watcher_task = asyncio.create_task(
            self._health_watcher_loop(), name="sandbox-health-watcher"
        )

    async def stop_health_watcher(self) -> None:
        task = self._watcher_task
        if task is None:
            return
        self._watcher_task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def check_health_once(self) -> None:
        """Single pass over cached workspaces. Exposed for tests so they can
        drive the watcher synchronously instead of waiting on the interval."""
        # Snapshot to avoid mutating during iteration if a handler restarts.
        for workspace_id, handle in list(self._workspaces.items()):
            try:
                status = await self._backend.status(handle)
            except SandboxError as exc:
                logger.warning(
                    "sandbox_health_probe_failed",
                    workspace_id=workspace_id,
                    error=str(exc),
                )
                continue
            await self._react_to_status(workspace_id, handle, status)

    async def _react_to_status(
        self,
        workspace_id: str,
        handle: SandboxHandle,
        status: SandboxStatus,
    ) -> None:
        if status.state in _DEAD_STATES:
            already = self._reported_crashes.get(workspace_id)
            if already == handle.id:
                return  # already surfaced for this container
            self._reported_crashes[workspace_id] = handle.id
            crash = WorkspaceCrash(
                workspace_id=workspace_id,
                sandbox_id=handle.id,
                state=status.state,
                exit_code=status.exit_code,
            )
            logger.info(
                "sandbox_workspace_crashed",
                workspace_id=workspace_id,
                sandbox_id=handle.id,
                state=status.state.value,
                exit_code=status.exit_code,
            )
            for handler in list(self._crash_handlers):
                try:
                    await handler(crash)
                except Exception as exc:  # noqa: BLE001 -- handler isolation
                    logger.warning(
                        "sandbox_crash_handler_failed",
                        workspace_id=workspace_id,
                        error=str(exc),
                    )
        else:
            # The same container reports `running` again (the in-place flip is
            # rare — typically the next poll just sees a fresh handle after
            # `restart_workspace` already cleared the entry). Clear so a later
            # crash on this same sandbox surfaces instead of being deduped.
            current = self._reported_crashes.get(workspace_id)
            if current == handle.id:
                self._reported_crashes.pop(workspace_id, None)

    async def _health_watcher_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._watcher_interval)
                await self.check_health_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- watcher must not die
                logger.warning("sandbox_health_watcher_error", error=str(exc))

    # --- shutdown ----------------------------------------------------------

    async def shutdown(self) -> None:
        await self.stop_health_watcher()
        for handle in list(self._workspaces.values()):
            await self._safe_remove(handle)
        self._workspaces.clear()

    async def _safe_remove(self, handle: SandboxHandle) -> None:
        """Removal must never raise into the agent — a sandbox that is already
        dead is exactly the case we are recovering from."""
        try:
            await self._backend.remove(handle)
        except SandboxError as exc:
            logger.warning("sandbox_remove_failed", sandbox_id=handle.id, error=str(exc))
