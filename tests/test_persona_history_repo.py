"""Unit tests for the persona_history repository (Plan 36).

`persona_history` is an append-only audit log: every persona create/update
writes one row capturing `{name, soul, identity, agents}` at the time of
the write. The repo exposes three primitives — `write_snapshot`,
`list_for_persona`, `get` — that the personas repo and the routes layer
build on.
"""
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.repository import persona_history as history_repo
from hermes.repository import personas as personas_repo
from hermes.repository.models import Persona


def _make_persona(
    *,
    id: int = 1,
    name: str = "Hermes",
    soul: str = "soul-text",
    identity: str = "identity-text",
    agents: str = "agents-text",
    is_default: bool = False,
    created_at: int = 1700000000,
    updated_at: int = 1700000000,
) -> Persona:
    return Persona(
        id=id,
        name=name,
        soul=soul,
        identity=identity,
        agents=agents,
        is_default=is_default,
        created_at=created_at,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_write_snapshot_returns_row_with_json_body(
    conn: AsyncEngine,
) -> None:
    # Need a real persona FK target — create one via the personas repo.
    persona = await personas_repo.create(
        conn,
        user_id=1,
        name="Hermes",
        soul="be calm",
        identity="be useful",
        agents="be tool-savvy",
        is_default=True,
        ts=1700000000,
    )

    row = await history_repo.write_snapshot(conn, persona, ts=1700000001)

    assert row.id > 0
    assert row.persona_id == persona.id
    assert row.created_at == 1700000001

    body = json.loads(row.snapshot_json)
    assert body == {
        "name": "Hermes",
        "soul": "be calm",
        "identity": "be useful",
        "agents": "be tool-savvy",
    }
    # `is_default` is a sorting flag, not identity — must not leak in.
    assert "is_default" not in body


@pytest.mark.asyncio
async def test_write_snapshot_default_author(conn: AsyncEngine) -> None:
    persona = await personas_repo.create(
        conn,
        user_id=1,
        name="A",
        soul="",
        identity="i",
        agents="",
        is_default=True,
        ts=1700000000,
    )
    row = await history_repo.write_snapshot(conn, persona, ts=1700000001)
    assert row.author == "user"


@pytest.mark.asyncio
async def test_write_snapshot_explicit_author(conn: AsyncEngine) -> None:
    persona = await personas_repo.create(
        conn,
        user_id=1,
        name="A",
        soul="",
        identity="i",
        agents="",
        is_default=True,
        ts=1700000000,
    )
    row = await history_repo.write_snapshot(
        conn, persona, author="system", ts=1700000001
    )
    assert row.author == "system"


@pytest.mark.asyncio
async def test_list_for_persona_newest_first(conn: AsyncEngine) -> None:
    persona = await personas_repo.create(
        conn,
        user_id=1,
        name="A",
        soul="",
        identity="i0",
        agents="",
        is_default=True,
        ts=1700000000,
    )

    # `create` writes an automatic snapshot too; we layer three more
    # explicit ones on top with strictly increasing timestamps so the
    # ordering is deterministic regardless of the auto-row.
    await history_repo.write_snapshot(conn, persona, ts=1700000010)
    middle = await history_repo.write_snapshot(conn, persona, ts=1700000020)
    newest = await history_repo.write_snapshot(conn, persona, ts=1700000030)

    rows = await history_repo.list_for_persona(conn, persona.id)
    assert len(rows) >= 3
    # Top three rows must be the explicit ones in DESC order.
    assert rows[0].id == newest.id
    assert rows[1].id == middle.id
    assert rows[0].created_at >= rows[1].created_at >= rows[2].created_at


@pytest.mark.asyncio
async def test_list_for_persona_empty(conn: AsyncEngine) -> None:
    # No persona with id=9999 exists ⇒ no history rows.
    assert await history_repo.list_for_persona(conn, 9999) == []


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_id(conn: AsyncEngine) -> None:
    assert await history_repo.get(conn, 9999) is None
