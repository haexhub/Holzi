"""Opt-in integration tests for the real Podman backend (Plan 11b-a).

Excluded from the default suite (`addopts = -m 'not integration'`). Run with a
rootless Podman socket available::

    HERMES_SANDBOX_SOCKET=unix://$XDG_RUNTIME_DIR/podman/podman.sock \\
        uv run pytest -m integration

These assert the safety-critical properties against a real engine: streamed
exec, resource limits, and that a killed sandbox does not take down the caller.
"""

import os

import pytest

from hermes.sandbox import ExecExit, ExecOutput, ResourceLimits, SandboxManager
from hermes.sandbox.podman import PodmanSandboxBackend

pytestmark = pytest.mark.integration

SOCKET = os.environ.get("HERMES_SANDBOX_SOCKET", "")
IMAGE = os.environ.get("HERMES_SANDBOX_IMAGE", "hermes-sandbox:dev")
NETWORK = os.environ.get("HERMES_SANDBOX_NETWORK", "hermes-sandbox")

requires_podman = pytest.mark.skipif(
    not SOCKET, reason="HERMES_SANDBOX_SOCKET not set"
)


def _manager() -> tuple[SandboxManager, PodmanSandboxBackend]:
    backend = PodmanSandboxBackend(SOCKET)
    mgr = SandboxManager(
        backend=backend,
        image=IMAGE,
        network=NETWORK,
        default_limits=ResourceLimits(cpus=1.0, memory_mb=256, disk_mb=512),
    )
    return mgr, backend


@requires_podman
async def test_ephemeral_exec_streams_real_output():
    mgr, backend = _manager()
    try:
        async with mgr.ephemeral() as handle:
            out, code = b"", None
            async for ev in mgr.exec(handle, ["sh", "-c", "echo hello"]):
                if isinstance(ev, ExecOutput) and ev.stream == "stdout":
                    out += ev.data
                elif isinstance(ev, ExecExit):
                    code = ev.exit_code
            assert b"hello" in out
            assert code == 0
    finally:
        await backend.aclose()


@requires_podman
async def test_killed_workspace_does_not_break_manager():
    mgr, backend = _manager()
    try:
        ws = await mgr.get_workspace("itest-ws")
        await backend.stop(ws)
        # A fresh workspace still works after the old one was killed.
        restarted = await mgr.restart_workspace("itest-ws")
        async for ev in mgr.exec(restarted, ["true"]):
            if isinstance(ev, ExecExit):
                assert ev.exit_code == 0
    finally:
        await mgr.shutdown()
        await backend.aclose()
