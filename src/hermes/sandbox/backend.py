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
    DirEntry,
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

    async def read_file(self, handle: SandboxHandle, path: str) -> bytes:
        """Read `path` from the sandbox volume. Raises `SandboxFileNotFound`
        if the path does not exist and `SandboxFileTooLarge` if the file is
        larger than the per-call cap enforced by the backend."""
        ...

    async def write_file(
        self, handle: SandboxHandle, path: str, data: bytes
    ) -> None:
        """Write `data` to `path` in the sandbox volume, creating parent
        directories as needed. Raises `SandboxFileTooLarge` if `data` exceeds
        the per-call cap."""
        ...

    async def list_dir(
        self, handle: SandboxHandle, path: str
    ) -> list[DirEntry]:
        """Shallow listing of the directory at `path` in the sandbox volume.

        Returns one `DirEntry` per immediate child (not recursive). Raises
        `SandboxFileNotFound` if the path does not exist, `SandboxError` if
        the path exists but is not a directory, and `SandboxNotRunning` if
        the sandbox is not in a runnable state. Order is backend-defined —
        callers that need a stable presentation sort themselves."""
        ...

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        ...

    async def stop(self, handle: SandboxHandle) -> None:
        ...

    async def remove(self, handle: SandboxHandle) -> None:
        ...
