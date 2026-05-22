from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import todos


async def test_add_todo_returns_dataclass(conn: AsyncEngine) -> None:
    t = await todos.add(conn, content="buy milk", tags="grocery,urgent", ts=1000)
    assert t.id > 0
    assert t.content == "buy milk"
    assert t.tags == "grocery,urgent"
    assert t.done_at is None
    assert t.created_at == 1000


async def test_list_all_returns_only_open_by_default(
    conn: AsyncEngine,
) -> None:
    a = await todos.add(conn, content="a", ts=1000)
    b = await todos.add(conn, content="b", ts=1001)
    await todos.mark_done(conn, a.id, ts=2000)

    open_only = await todos.list_all(conn)
    assert [t.id for t in open_only] == [b.id]


async def test_list_all_with_only_open_false_returns_done_too(
    conn: AsyncEngine,
) -> None:
    a = await todos.add(conn, content="a", ts=1000)
    b = await todos.add(conn, content="b", ts=1001)
    await todos.mark_done(conn, a.id, ts=2000)

    all_todos = await todos.list_all(conn, only_open=False)
    assert {t.id for t in all_todos} == {a.id, b.id}


async def test_list_all_filters_by_tag(conn: AsyncEngine) -> None:
    a = await todos.add(conn, content="a", tags="work,urgent", ts=1)
    b = await todos.add(conn, content="b", tags="home", ts=2)
    c = await todos.add(conn, content="c", tags="work", ts=3)

    work = await todos.list_all(conn, tag="work")
    assert {t.id for t in work} == {a.id, c.id}

    home = await todos.list_all(conn, tag="home")
    assert {t.id for t in home} == {b.id}


async def test_mark_done_returns_true_first_time_false_second(
    conn: AsyncEngine,
) -> None:
    t = await todos.add(conn, content="x", ts=1000)
    assert await todos.mark_done(conn, t.id, ts=2000) is True
    assert await todos.mark_done(conn, t.id, ts=3000) is False

    fetched = await todos.get(conn, t.id)
    assert fetched is not None
    assert fetched.done_at == 2000


async def test_mark_done_returns_false_for_unknown_id(
    conn: AsyncEngine,
) -> None:
    assert await todos.mark_done(conn, 99999, ts=1000) is False
