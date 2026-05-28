"""In-memory sandbox backend for unit tests.

Models container lifecycle, scriptable exec output, and simulable crash/OOM,
with live-handle accounting so leak tests can assert cleanup. No containers,
no Podman — this is the double the manager tests run against.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from hermes.sandbox.errors import (
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxNotRunning,
)
from hermes.sandbox.models import (
    FILE_SIZE_CAP,
    ExecEvent,
    ExecExit,
    SandboxHandle,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)

# Re-export under the legacy name so existing tests don't need to switch import.
FAKE_FILE_SIZE_CAP = FILE_SIZE_CAP


@dataclass
class _Container:
    id: str
    spec: SandboxSpec
    state: SandboxState
    networks: set[str] = field(default_factory=set)
    exit_code: int | None = None
    files: dict[str, bytes] = field(default_factory=dict)


class FakeSandboxBackend:
    def __init__(self) -> None:
        self._containers: dict[str, _Container] = {}
        self._counter = 0
        # Output for the *next* exec call; consumed once, then reset to default.
        self._exec_script: list[ExecEvent] | None = None

    # --- SandboxBackend ----------------------------------------------------

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        self._counter += 1
        cid = f"fake-{self._counter}"
        self._containers[cid] = _Container(
            id=cid, spec=spec, state=SandboxState.running, networks={spec.network}
        )
        return SandboxHandle(id=cid, kind=spec.kind, spec=spec)

    async def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> AsyncIterator[ExecEvent]:
        container = self._containers.get(handle.id)
        if container is None or container.state is not SandboxState.running:
            raise SandboxNotRunning(f"sandbox {handle.id} is not running")
        events = self._exec_script if self._exec_script is not None else [ExecExit(exit_code=0)]
        self._exec_script = None
        for ev in events:
            yield ev

    async def read_file(self, handle: SandboxHandle, path: str) -> bytes:
        container = self._require_running(handle)
        if path not in container.files:
            raise SandboxFileNotFound(f"{path} not found in sandbox {handle.id}")
        data = container.files[path]
        if len(data) > FAKE_FILE_SIZE_CAP:
            raise SandboxFileTooLarge(
                f"{path} is {len(data)} bytes, cap is {FAKE_FILE_SIZE_CAP}"
            )
        return data

    async def write_file(
        self, handle: SandboxHandle, path: str, data: bytes
    ) -> None:
        container = self._require_running(handle)
        if len(data) > FAKE_FILE_SIZE_CAP:
            raise SandboxFileTooLarge(
                f"write to {path} is {len(data)} bytes, cap is {FAKE_FILE_SIZE_CAP}"
            )
        container.files[path] = bytes(data)

    def _require_running(self, handle: SandboxHandle) -> _Container:
        container = self._containers.get(handle.id)
        if container is None or container.state is not SandboxState.running:
            raise SandboxNotRunning(f"sandbox {handle.id} is not running")
        return container

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        container = self._containers.get(handle.id)
        if container is None:
            return SandboxStatus(state=SandboxState.removed)
        return SandboxStatus(state=container.state, exit_code=container.exit_code)

    async def stop(self, handle: SandboxHandle) -> None:
        container = self._containers.get(handle.id)
        if container is not None and container.state is SandboxState.running:
            container.state = SandboxState.exited
            container.exit_code = 0

    async def remove(self, handle: SandboxHandle) -> None:
        container = self._containers.get(handle.id)
        if container is not None:
            container.state = SandboxState.removed

    # --- test scripting helpers -------------------------------------------

    def script_exec(self, events: Sequence[ExecEvent]) -> None:
        """Set the events the next exec call will stream."""
        self._exec_script = list(events)

    def simulate_crash(self, sandbox_id: str) -> None:
        self._containers[sandbox_id].state = SandboxState.crashed

    def simulate_oom(self, sandbox_id: str) -> None:
        self._containers[sandbox_id].state = SandboxState.oom

    def live_count(self) -> int:
        return sum(
            1 for c in self._containers.values() if c.state is not SandboxState.removed
        )

    def networks_of(self, sandbox_id: str) -> set[str]:
        return set(self._containers[sandbox_id].networks)
