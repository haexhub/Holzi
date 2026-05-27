"""Wire a SandboxManager from settings.

Kept separate so `manager.py` stays runtime-neutral (it never imports Podman).
Returns ``None`` when no sandbox socket is configured: the agent then boots
without a sandbox, and any future tool that needs one must fail loudly rather
than execute in-process.
"""

from __future__ import annotations

from hermes.config import Settings
from hermes.sandbox.manager import SandboxManager
from hermes.sandbox.models import ResourceLimits
from hermes.sandbox.podman import PodmanSandboxBackend


def build_sandbox_manager(
    settings: Settings,
) -> tuple[SandboxManager, PodmanSandboxBackend] | None:
    if not settings.sandbox_socket:
        return None
    backend = PodmanSandboxBackend(settings.sandbox_socket)
    manager = SandboxManager(
        backend=backend,
        image=settings.sandbox_image,
        network=settings.sandbox_network,
        default_limits=ResourceLimits(
            cpus=settings.sandbox_cpus,
            memory_mb=settings.sandbox_memory_mb,
            disk_mb=settings.sandbox_disk_mb,
        ),
    )
    return manager, backend
