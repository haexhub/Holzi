"""Sandbox runtime (Plan 11b-a).

Isolated containers for code execution / shell / unbounded writes, so the agent
container stays unkillable. Runtime is rootless Podman via the Docker-compatible
socket; all code here is runtime-neutral behind `SandboxBackend`.
"""

from hermes.sandbox.backend import SandboxBackend
from hermes.sandbox.errors import (
    SandboxError,
    SandboxFileNotFound,
    SandboxFileTooLarge,
    SandboxNotRunning,
)
from hermes.sandbox.manager import SandboxManager, WorkspaceCrash
from hermes.sandbox.models import (
    ExecEvent,
    ExecExit,
    ExecOutput,
    ResourceLimits,
    SandboxHandle,
    SandboxKind,
    SandboxSpec,
    SandboxState,
    SandboxStatus,
)

__all__ = [
    "ExecEvent",
    "ExecExit",
    "ExecOutput",
    "ResourceLimits",
    "SandboxBackend",
    "SandboxError",
    "SandboxFileNotFound",
    "SandboxFileTooLarge",
    "SandboxHandle",
    "SandboxKind",
    "SandboxManager",
    "SandboxNotRunning",
    "SandboxSpec",
    "SandboxState",
    "SandboxStatus",
    "WorkspaceCrash",
]
