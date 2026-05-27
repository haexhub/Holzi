"""The runtime-neutral backend contract.

`SandboxManager` drives a `SandboxBackend`; `PodmanSandboxBackend` is the
production implementation and `FakeSandboxBackend` the in-memory test double.
Keeping this a Protocol (not an ABC) lets the fake stay a plain object and
keeps the dependency direction pointing at this module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol, runtime_checkable

from hermes.sandbox.models import (
    ExecEvent,
    SandboxHandle,
    SandboxSpec,
    SandboxStatus,
)


@runtime_checkable
class SandboxBackend(Protocol):
    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Create and start a container for `spec`, attached only to
        `spec.network` and capped by `spec.limits`."""
        ...

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> AsyncIterator[ExecEvent]:
        """Run `argv` in the sandbox, streaming stdout/stderr as ExecOutput
        events followed by a single ExecExit. Raises `SandboxNotRunning` if the
        sandbox is not in a runnable state."""
        ...

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        ...

    async def stop(self, handle: SandboxHandle) -> None:
        ...

    async def remove(self, handle: SandboxHandle) -> None:
        ...
