import json
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import conversations, reminders
from hermes.scheduler import ConversationSweepScheduler, ReminderScheduler
from hermes.signal.client import SignalClient

SELF_NUMBER = "+491701234567"


def _make_signal_client(sends: list[dict[str, Any]] | None = None) -> SignalClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/send":
            if sends is not None:
                sends.append(json.loads(request.content))
            return httpx.Response(201, json={"timestamp": 1700000000000})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://fake-signal")
    return SignalClient(http, SELF_NUMBER)


async def test_fire_due_sends_and_marks_only_past_reminders(
    conn: AsyncEngine,
) -> None:
    past = await reminders.create(conn, due_at=1000, message="past", ts=500)
    future = await reminders.create(conn, due_at=5000, message="future", ts=500)

    sends: list[dict[str, Any]] = []
    sched = ReminderScheduler(conn, _make_signal_client(sends), SELF_NUMBER)
    fired = await sched.fire_due(now=2000)

    assert fired == 1
    assert [s["message"] for s in sends] == ["past"]

    all_reminders = await reminders.list_all(conn, include_fired=True)
    by_id = {r.id: r for r in all_reminders}
    assert by_id[past.id].fired_at == 2000
    assert by_id[future.id].fired_at is None


async def test_fire_due_skips_when_signal_disabled(conn: AsyncEngine) -> None:
    await reminders.create(conn, due_at=1000, message="x", ts=500)

    sched = ReminderScheduler(conn, None, None)
    fired = await sched.fire_due(now=2000)

    assert fired == 0
    pending = await reminders.list_all(conn)
    assert len(pending) == 1  # still pending


async def test_fire_due_skips_non_signal_channels(conn: AsyncEngine) -> None:
    await reminders.create(conn, due_at=1000, message="web", channel="web", ts=500)

    sends: list[dict[str, Any]] = []
    sched = ReminderScheduler(conn, _make_signal_client(sends), SELF_NUMBER)
    fired = await sched.fire_due(now=2000)

    assert fired == 0
    assert sends == []


async def test_fire_due_leaves_reminder_pending_when_send_fails(
    conn: AsyncEngine,
) -> None:
    await reminders.create(conn, due_at=1000, message="boom", ts=500)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    failing = SignalClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fake"),
        SELF_NUMBER,
    )
    sched = ReminderScheduler(conn, failing, SELF_NUMBER)
    fired = await sched.fire_due(now=2000)

    assert fired == 0
    pending = await reminders.list_all(conn)
    assert len(pending) == 1  # next tick will retry


# ---------------------------------------------------------------------------
# ConversationSweepScheduler
# ---------------------------------------------------------------------------


async def test_conversation_sweep_deletes_expired_and_keeps_bookmarked(
    conn: AsyncEngine, tmp_path: Path
) -> None:
    expired = await conversations.create(conn, channel="web", ts=0)
    pinned = await conversations.create(
        conn, channel="web", ts=0, bookmarked=True
    )
    fresh = await conversations.create(conn, channel="web", ts=10_000_000)

    scratch_root = tmp_path / "conversations"
    scratch_root.mkdir()
    (scratch_root / str(expired.id)).mkdir()

    sweeper = ConversationSweepScheduler(conn, scratch_root)
    deleted = await sweeper.sweep(now=expired.expires_at + 1)  # type: ignore[operator]

    assert deleted == [expired.id]
    assert await conversations.get(conn, expired.id) is None
    assert await conversations.get(conn, pinned.id) is not None
    assert await conversations.get(conn, fresh.id) is not None
    assert not (scratch_root / str(expired.id)).exists()


async def test_conversation_sweep_noop_when_nothing_expired(
    conn: AsyncEngine, tmp_path: Path
) -> None:
    await conversations.create(conn, channel="web", ts=10_000_000)
    sweeper = ConversationSweepScheduler(conn, tmp_path / "conversations")
    assert await sweeper.sweep(now=10_000_001) == []
