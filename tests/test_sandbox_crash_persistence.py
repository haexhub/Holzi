"""Plan 20-A: the lifespan-registered persistence handler turns every
`WorkspaceCrash` the SandboxManager fires into a `sandbox_crashes` row.

These tests drive the manager via a `FakeSandboxBackend` so we can prove
the edge between "watcher saw a dead state" and "repository has a row"
without depending on a live Podman socket. Re-uses the existing test
contract (the manager's dedupe, the OOM path, restart re-firing) so this
test catches drift in either layer.
"""
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import sandbox_crashes as repo
from hermes.sandbox import (
    ResourceLimits,
    SandboxManager,
    WorkspaceCrash,
)
from hermes.sandbox.fake import FakeSandboxBackend

SANDBOX_NETWORK = "none"
LIMITS = ResourceLimits(cpus=1.0, memory_mb=512, disk_mb=1024)


def make_manager() -> tuple[SandboxManager, FakeSandboxBackend]:
    backend = FakeSandboxBackend()
    mgr = SandboxManager(
        backend=backend,
        image="hermes-sandbox:test",
        network=SANDBOX_NETWORK,
        default_limits=LIMITS,
    )
    return mgr, backend


def make_persist_handler(engine: AsyncEngine, *, now: int):
    """Build the handler `main.py` registers, but with an injected
    timestamp so the test doesn't depend on wall-clock time."""

    async def persist(crash: WorkspaceCrash) -> None:
        await repo.insert(
            engine,
            workspace_id=crash.workspace_id,
            sandbox_id=crash.sandbox_id,
            crashed_at=now,
            state=crash.state.value,
            exit_code=crash.exit_code,
        )

    return persist


async def test_persist_handler_writes_one_row_per_crash(conn: AsyncEngine) -> None:
    mgr, backend = make_manager()
    mgr.add_crash_handler(make_persist_handler(conn, now=42))

    handle = await mgr.get_workspace("ws-1")
    backend.simulate_crash(handle.id)
    await mgr.check_health_once()

    rows = await repo.list_recent(conn)
    assert len(rows) == 1
    assert rows[0].workspace_id == "ws-1"
    assert rows[0].sandbox_id == handle.id
    assert rows[0].state == "crashed"
    assert rows[0].crashed_at == 42


async def test_persist_handler_dedupes_through_manager(conn: AsyncEngine) -> None:
    """The manager already dedupes per `(workspace_id, sandbox_id)`. The
    persistence handler relies on that — multiple health ticks against
    the same dead sandbox must yield exactly one row."""
    mgr, backend = make_manager()
    mgr.add_crash_handler(make_persist_handler(conn, now=42))

    handle = await mgr.get_workspace("ws-1")
    backend.simulate_crash(handle.id)
    await mgr.check_health_once()
    await mgr.check_health_once()
    await mgr.check_health_once()

    rows = await repo.list_recent(conn)
    assert len(rows) == 1


async def test_persist_handler_records_oom_with_null_exit_code(
    conn: AsyncEngine,
) -> None:
    mgr, backend = make_manager()
    mgr.add_crash_handler(make_persist_handler(conn, now=42))

    handle = await mgr.get_workspace("ws-1")
    backend.simulate_oom(handle.id)
    await mgr.check_health_once()

    rows = await repo.list_recent(conn)
    assert len(rows) == 1
    assert rows[0].state == "oom"
    # The OOM transition has no clean exit code — must round-trip as None.
    assert rows[0].exit_code is None


async def test_persist_handler_records_each_restart_as_new_row(
    conn: AsyncEngine,
) -> None:
    """`restart_workspace` clears the manager's dedupe entry, so a crash
    on the fresh container fires another event — and we must persist a
    new row, not skip it as a "duplicate"."""
    mgr, backend = make_manager()
    mgr.add_crash_handler(make_persist_handler(conn, now=42))

    first = await mgr.get_workspace("ws-1")
    backend.simulate_crash(first.id)
    await mgr.check_health_once()

    fresh = await mgr.restart_workspace("ws-1")
    backend.simulate_crash(fresh.id)
    await mgr.check_health_once()

    rows = await repo.list_recent(conn)
    assert len(rows) == 2
    # Newest first; the fresh sandbox_id is at the top.
    assert rows[0].sandbox_id == fresh.id
    assert rows[1].sandbox_id == first.id
