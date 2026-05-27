"""SandboxManager — the single owner of sandbox lifecycle.

Lives on `app.state` (single-worker / single-user invariant, like the existing
`chat_runs` and `approvals` registries). Workspace sandboxes auto-start on
first use and persist; ephemeral sandboxes are created per call and always
removed. The manager is the only place that builds a `SandboxSpec`, so it is
the chokepoint that guarantees every sandbox gets resource limits and lands on
the isolated network — never the agent's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

import structlog

from hermes.sandbox.backend import SandboxBackend
from hermes.sandbox.errors import SandboxError
from hermes.sandbox.models import (
    ExecEvent,
    ResourceLimits,
    SandboxHandle,
    SandboxKind,
    SandboxSpec,
    SandboxStatus,
)

logger = structlog.get_logger(__name__)


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

    async def get_workspace(self, workspace_id: str) -> SandboxHandle:
        """Return the workspace sandbox, starting it on first use."""
        existing = self._workspaces.get(workspace_id)
        if existing is not None:
            return existing
        handle = await self._backend.create(self._workspace_spec(workspace_id))
        self._workspaces[workspace_id] = handle
        logger.info("sandbox_workspace_started", workspace_id=workspace_id, sandbox_id=handle.id)
        return handle

    async def restart_workspace(self, workspace_id: str) -> SandboxHandle:
        """Tear down the (possibly dead) workspace sandbox and start a fresh one."""
        existing = self._workspaces.pop(workspace_id, None)
        if existing is not None:
            await self._safe_remove(existing)
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

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        return await self._backend.status(handle)

    # --- shutdown ----------------------------------------------------------

    async def shutdown(self) -> None:
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
