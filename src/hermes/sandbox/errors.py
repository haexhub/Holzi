"""Typed sandbox errors.

The contract: a misbehaving sandbox surfaces as one of these *catchable*
exceptions, never as an uncontrolled failure that could tear down the agent.
"""

from __future__ import annotations


class SandboxError(Exception):
    """Base for all sandbox runtime failures."""


class SandboxNotRunning(SandboxError):
    """Raised when an operation targets a sandbox that has crashed, OOM'd,
    exited, or been removed. The caller is expected to catch this and (for
    workspaces) offer a restart rather than crash the agent."""


class SandboxFileTooLarge(SandboxError):
    """Raised when read_file/write_file exceeds the per-call size cap."""


class SandboxFileNotFound(SandboxError):
    """Raised when read_file targets a path that does not exist in the sandbox."""
