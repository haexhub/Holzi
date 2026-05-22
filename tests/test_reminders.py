import aiosqlite

from hermes.repository import reminders


async def test_create_reminder_returns_dataclass(conn: aiosqlite.Connection) -> None:
    r = await reminders.create(conn, due_at=2000, message="standup", ts=1000)
    assert r.id > 0
    assert r.due_at == 2000
    assert r.message == "standup"
    assert r.channel == "signal"
    assert r.fired_at is None
    assert r.created_at == 1000


async def test_list_all_returns_pending_only_by_default(
    conn: aiosqlite.Connection,
) -> None:
    a = await reminders.create(conn, due_at=2000, message="a", ts=1000)
    b = await reminders.create(conn, due_at=3000, message="b", ts=1000)
    await reminders.mark_fired(conn, a.id, ts=2500)

    pending = await reminders.list_all(conn)
    assert [r.id for r in pending] == [b.id]

    everything = await reminders.list_all(conn, include_fired=True)
    assert {r.id for r in everything} == {a.id, b.id}


async def test_list_due_returns_only_pending_and_due(
    conn: aiosqlite.Connection,
) -> None:
    past = await reminders.create(conn, due_at=1000, message="past", ts=500)
    future = await reminders.create(conn, due_at=9000, message="future", ts=500)
    fired = await reminders.create(conn, due_at=1500, message="fired", ts=500)
    await reminders.mark_fired(conn, fired.id, ts=2000)

    due = await reminders.list_due(conn, now=2000)
    assert [r.id for r in due] == [past.id]
    _ = future  # silence


async def test_mark_fired_sets_fired_at(conn: aiosqlite.Connection) -> None:
    r = await reminders.create(conn, due_at=2000, message="x", ts=1000)
    await reminders.mark_fired(conn, r.id, ts=2500)

    pending = await reminders.list_all(conn, include_fired=True)
    assert pending[0].fired_at == 2500


async def test_mark_fired_preserves_existing_fired_at(
    conn: aiosqlite.Connection,
) -> None:
    r = await reminders.create(conn, due_at=2000, message="x", ts=1000)
    await reminders.mark_fired(conn, r.id, ts=2500)
    await reminders.mark_fired(conn, r.id, ts=9999)

    everything = await reminders.list_all(conn, include_fired=True)
    assert everything[0].fired_at == 2500
