"""In-memory sandbox backend for unit tests.

Models container lifecycle, scriptable exec output, and simulable crash/OOM,
with live-handle accounting so leak tests can assert cleanup. No containers,
no Podman — this is the double the manager tests run against.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from hermes.sandbox.errors import (
    SandboxError,
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxNotRunning,
)
from hermes.sandbox.models import (
    FILE_SIZE_CAP,
    DirEntry,
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
        # FIFO of scripted outputs — each entry is the events for one exec call.
        # When empty, exec falls back to a clean exit. The queue lets tests that
        # invoke several `exec`s in a row (e.g. `git status` followed by
        # `git add` + `git commit`) script each one in order.
        self._exec_scripts: list[list[ExecEvent]] = []
        # Append-only log of every argv that has been exec'd. Tests inspect
        # this to assert (e.g.) that a workspace write actually issued the
        # expected `git add` + `git commit` pair with the right message.
        self.recorded_execs: list[list[str]] = []

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
        self.recorded_execs.append(list(argv))
        events = (
            self._exec_scripts.pop(0)
            if self._exec_scripts
            else [ExecExit(exit_code=0)]
        )
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

    async def list_dir(
        self, handle: SandboxHandle, path: str
    ) -> list[DirEntry]:
        container = self._require_running(handle)
        # Directories don't exist as first-class entries in the fake — they
        # are inferred from the `files` map. Normalise the query path to
        # `/...` with no trailing slash so prefix matching is unambiguous.
        if not path.startswith("/"):
            raise SandboxError(f"list_dir path must be absolute: {path}")
        normalised = path.rstrip("/") or "/"
        prefix = "/" if normalised == "/" else normalised + "/"

        # A path is treated as an existing directory if any stored file lives
        # under it. The exact path being a file is rejected — the Podman
        # backend rejects this case too and the API layer relies on it.
        if normalised in container.files:
            raise SandboxError(f"{path} is not a directory")
        has_children = any(
            stored.startswith(prefix) for stored in container.files
        )
        if not has_children:
            # `/workspace` is always present in a real workspace sandbox (it's
            # the mounted volume), even when nothing has been written yet —
            # mirror that so the API contract for an empty workspace is
            # "200 with empty entries", not "404".
            if normalised == "/workspace":
                return []
            raise SandboxFileNotFound(
                f"{path} not found in sandbox {handle.id}"
            )

        # Collect the *next* segment after the prefix for every stored file:
        # if the file lives directly under `path`, it's a file entry; if
        # there's anything deeper, the next segment is a directory.
        files: dict[str, int] = {}
        dirs: set[str] = set()
        for stored, data in container.files.items():
            if not stored.startswith(prefix):
                continue
            rest = stored[len(prefix) :]
            if not rest:
                continue
            head, sep, _ = rest.partition("/")
            if sep:
                dirs.add(head)
            else:
                files[head] = len(data)
        # A name listed as both a dir and a file in the fake's flat map is
        # impossible in real POSIX — collapse to dir so the test double
        # mirrors what the Podman backend would observe.
        entries: list[DirEntry] = []
        for name in sorted(dirs):
            entries.append(DirEntry(name=name, entry_type="dir", size=0))
        for name in sorted(files):
            if name in dirs:
                continue
            entries.append(
                DirEntry(name=name, entry_type="file", size=files[name])
            )
        return entries

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
        """Enqueue the events for the next un-scripted exec call. Subsequent
        calls each consume the next entry in FIFO order; an exec without a
        scripted entry falls back to a clean exit."""
        self._exec_scripts.append(list(events))

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
