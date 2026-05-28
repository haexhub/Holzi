"""Runtime-neutral value types for the sandbox spine (Plan 11b-a).

These describe *what* a sandbox is and what an exec stream looks like, with no
reference to Podman/Docker. The concrete backend translates a `SandboxSpec`
into container-create options and demuxes its exec stream into `ExecEvent`s.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class SandboxKind(StrEnum):
    """Workspace sandboxes persist per workspace; ephemeral ones are one-shot."""

    workspace = "workspace"
    ephemeral = "ephemeral"


class SandboxState(StrEnum):
    running = "running"
    exited = "exited"
    crashed = "crashed"
    # OOM is split out from `crashed` because it is the recoverable signal the
    # 11b-b health watcher surfaces as `sandbox_crashed` with a restart action.
    oom = "oom"
    removed = "removed"


@dataclass(frozen=True)
class ResourceLimits:
    """Mandatory per-container caps. There is deliberately no "unlimited" path:
    constructing limits with a non-positive value is a programming error."""

    cpus: float
    memory_mb: int
    disk_mb: int

    def __post_init__(self) -> None:
        if self.cpus <= 0:
            raise ValueError("cpus must be > 0")
        if self.memory_mb <= 0:
            raise ValueError("memory_mb must be > 0")
        if self.disk_mb <= 0:
            raise ValueError("disk_mb must be > 0")


@dataclass(frozen=True)
class SandboxSpec:
    """A request to create one sandbox container."""

    kind: SandboxKind
    image: str
    network: str
    limits: ResourceLimits
    # Set iff kind is workspace; identifies the persistent workspace + volume.
    workspace_id: str | None = None
    volume: str | None = None

    def __post_init__(self) -> None:
        if self.kind is SandboxKind.workspace and not self.workspace_id:
            raise ValueError("workspace sandboxes require a workspace_id")
        if self.kind is SandboxKind.ephemeral and self.workspace_id is not None:
            raise ValueError("ephemeral sandboxes must not carry a workspace_id")


@dataclass(frozen=True)
class SandboxHandle:
    """Identifies a created sandbox container."""

    id: str
    kind: SandboxKind
    spec: SandboxSpec


@dataclass(frozen=True)
class SandboxStatus:
    state: SandboxState
    exit_code: int | None = None


@dataclass(frozen=True)
class ExecOutput:
    """One chunk of streamed exec output."""

    stream: Literal["stdout", "stderr"]
    data: bytes


@dataclass(frozen=True)
class ExecExit:
    """Terminal event of an exec stream, carrying the process exit code."""

    exit_code: int


# An exec stream yields zero or more ExecOutput chunks, then exactly one
# ExecExit. Consumers pattern-match on the event type.
ExecEvent = ExecOutput | ExecExit
