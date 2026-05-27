"""Tests for the sandbox runtime spine (Plan 11b-a).

These exercise the runtime-neutral layer — the `SandboxManager` driving a
`FakeSandboxBackend`. The real `PodmanSandboxBackend` talks to a rootless
Podman socket and is covered by opt-in integration tests, not here.
"""

import pytest

from hermes.sandbox import (
    ExecExit,
    ExecOutput,
    ResourceLimits,
    SandboxKind,
    SandboxManager,
    SandboxNotRunning,
    SandboxState,
)
from hermes.sandbox.fake import FakeSandboxBackend

SANDBOX_NETWORK = "hermes-sandbox"
LIMITS = ResourceLimits(cpus=1.0, memory_mb=512, disk_mb=1024)


def make_manager(
    backend: FakeSandboxBackend | None = None,
) -> tuple[SandboxManager, FakeSandboxBackend]:
    backend = backend or FakeSandboxBackend()
    mgr = SandboxManager(
        backend=backend,
        image="hermes-sandbox:test",
        network=SANDBOX_NETWORK,
        default_limits=LIMITS,
    )
    return mgr, backend


async def drain(events) -> tuple[bytes, bytes, int]:
    """Consume an exec event stream into (stdout, stderr, exit_code)."""
    out, err, code = b"", b"", None
    async for ev in events:
        if isinstance(ev, ExecOutput):
            if ev.stream == "stdout":
                out += ev.data
            else:
                err += ev.data
        elif isinstance(ev, ExecExit):
            code = ev.exit_code
    assert code is not None, "stream ended without an ExecExit"
    return out, err, code


# --- limits & network isolation (safety-critical) ---------------------------


async def test_every_spec_carries_resource_limits():
    """No unlimited path: every sandbox the manager builds has CPU/RAM/disk caps."""
    mgr, backend = make_manager()
    handle = await mgr.get_workspace("ws-1")
    assert handle.spec.limits == LIMITS
    assert handle.spec.limits.memory_mb > 0
    assert handle.spec.limits.cpus > 0
    assert handle.spec.limits.disk_mb > 0


async def test_resource_limits_reject_unlimited():
    """ResourceLimits cannot express an unbounded sandbox."""
    with pytest.raises(ValueError):
        ResourceLimits(cpus=0, memory_mb=512, disk_mb=1024)
    with pytest.raises(ValueError):
        ResourceLimits(cpus=1.0, memory_mb=0, disk_mb=1024)
    with pytest.raises(ValueError):
        ResourceLimits(cpus=1.0, memory_mb=512, disk_mb=0)


async def test_sandbox_attached_only_to_isolated_network():
    """Every sandbox lands on the dedicated network, never the agent's."""
    mgr, backend = make_manager()
    ws = await mgr.get_workspace("ws-1")
    assert ws.spec.network == SANDBOX_NETWORK
    async with mgr.ephemeral() as eph:
        assert eph.spec.network == SANDBOX_NETWORK
    # The fake records what each container was attached to.
    assert backend.networks_of(ws.id) == {SANDBOX_NETWORK}


# --- workspace lifecycle -----------------------------------------------------


async def test_workspace_autostarts_and_persists():
    """First use starts a workspace sandbox; repeat use returns the same one."""
    mgr, backend = make_manager()
    first = await mgr.get_workspace("ws-1")
    again = await mgr.get_workspace("ws-1")
    assert first.id == again.id
    assert first.kind is SandboxKind.workspace
    assert backend.live_count() == 1


async def test_workspace_spec_validation():
    """Workspace sandboxes are keyed by workspace id; ephemeral ones are not."""
    mgr, _ = make_manager()
    ws = await mgr.get_workspace("ws-1")
    assert ws.spec.workspace_id == "ws-1"
    # A named volume is what lets a workspace survive a restart.
    assert ws.spec.volume == "hermes-ws-ws-1"
    async with mgr.ephemeral() as eph:
        assert eph.kind is SandboxKind.ephemeral
        assert eph.spec.workspace_id is None
        # Ephemeral sandboxes intentionally have no persistent volume.
        assert eph.spec.volume is None


# --- ephemeral cleanup -------------------------------------------------------


async def test_ephemeral_cleaned_up_after_use():
    mgr, backend = make_manager()
    async with mgr.ephemeral():
        assert backend.live_count() == 1
    assert backend.live_count() == 0


async def test_ephemeral_cleaned_up_on_exception():
    mgr, backend = make_manager()
    with pytest.raises(RuntimeError):
        async with mgr.ephemeral():
            assert backend.live_count() == 1
            raise RuntimeError("boom in task")
    assert backend.live_count() == 0


async def test_no_leaked_handles_after_many_runs():
    mgr, backend = make_manager()
    for _ in range(25):
        async with mgr.ephemeral() as h:
            await drain(mgr.exec(h, ["echo", "hi"]))
    assert backend.live_count() == 0


# --- exec streaming ----------------------------------------------------------


async def test_exec_streams_stdout_stderr_and_exit_code():
    mgr, backend = make_manager()
    backend.script_exec(
        [
            ExecOutput("stdout", b"hello "),
            ExecOutput("stderr", b"warn\n"),
            ExecOutput("stdout", b"world\n"),
            ExecExit(exit_code=0),
        ]
    )
    handle = await mgr.get_workspace("ws-1")
    out, err, code = await drain(mgr.exec(handle, ["echo", "hello world"]))
    assert out == b"hello world\n"
    assert err == b"warn\n"
    assert code == 0


async def test_exec_reports_nonzero_exit():
    mgr, backend = make_manager()
    backend.script_exec([ExecOutput("stderr", b"nope\n"), ExecExit(exit_code=2)])
    handle = await mgr.get_workspace("ws-1")
    _, err, code = await drain(mgr.exec(handle, ["false"]))
    assert code == 2
    assert err == b"nope\n"


# --- crash / OOM resilience --------------------------------------------------


async def test_crashed_sandbox_does_not_kill_the_agent():
    """A crashed sandbox surfaces as a catchable error and dead status — it
    never propagates as an uncontrolled failure, and the manager keeps working."""
    mgr, backend = make_manager()
    handle = await mgr.get_workspace("ws-1")
    backend.simulate_crash(handle.id)

    status = await mgr.status(handle)
    assert status.state is SandboxState.crashed

    # exec against a dead sandbox raises a typed, catchable error...
    with pytest.raises(SandboxNotRunning):
        await drain(mgr.exec(handle, ["echo", "hi"]))

    # ...and the manager is still fully functional for other workspaces.
    other = await mgr.get_workspace("ws-2")
    backend.script_exec([ExecOutput("stdout", b"ok\n"), ExecExit(exit_code=0)])
    out, _, code = await drain(mgr.exec(other, ["echo", "ok"]))
    assert out == b"ok\n"
    assert code == 0


async def test_oom_is_a_distinct_recoverable_status():
    """An OOM is reported distinctly from a generic crash (the signal 11b-b's
    health watcher will consume) and does not raise on inspection."""
    mgr, backend = make_manager()
    handle = await mgr.get_workspace("ws-1")
    backend.simulate_oom(handle.id)
    status = await mgr.status(handle)
    assert status.state is SandboxState.oom
    assert status.state is not SandboxState.crashed


# --- restart -----------------------------------------------------------------


async def test_restart_replaces_crashed_workspace():
    mgr, backend = make_manager()
    original = await mgr.get_workspace("ws-1")
    backend.simulate_crash(original.id)

    restarted = await mgr.restart_workspace("ws-1")
    assert restarted.id != original.id
    assert (await mgr.status(restarted)).state is SandboxState.running
    # the dead one is gone, exactly one live workspace remains
    assert backend.live_count() == 1


# --- manager shutdown --------------------------------------------------------


async def test_shutdown_removes_all_workspaces():
    mgr, backend = make_manager()
    await mgr.get_workspace("ws-1")
    await mgr.get_workspace("ws-2")
    assert backend.live_count() == 2
    await mgr.shutdown()
    assert backend.live_count() == 0


# --- Docker stream demux (pure, no Podman) -----------------------------------


def _frame(stream_type: int, payload: bytes) -> bytes:
    import struct

    return struct.pack(">BxxxI", stream_type, len(payload)) + payload


def test_drain_frames_demuxes_stdout_and_stderr():
    from hermes.sandbox.podman import _drain_frames

    buffer = bytearray(_frame(1, b"out") + _frame(2, b"err"))
    events = _drain_frames(buffer)
    assert events == [ExecOutput("stdout", b"out"), ExecOutput("stderr", b"err")]
    assert len(buffer) == 0


def test_drain_frames_keeps_partial_frame_for_next_chunk():
    """The highest-risk real-Podman path: a frame split across two read chunks."""
    from hermes.sandbox.podman import _drain_frames

    frame = _frame(1, b"hello")
    buffer = bytearray(frame[:3])  # header not even complete yet
    assert _drain_frames(buffer) == []
    buffer.extend(frame[3:6])  # header complete, payload partial
    assert _drain_frames(buffer) == []
    buffer.extend(frame[6:])  # remainder arrives
    assert _drain_frames(buffer) == [ExecOutput("stdout", b"hello")]
    assert len(buffer) == 0
