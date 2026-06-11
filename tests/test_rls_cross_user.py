"""Cross-user RLS denial smoke test (Task 19, §1 security floor).

The design doc mandates "an RLS smoke test (cross-user denial) before opening
to a second user." This file proves, for every one of the 11 personal tables
that `0003_rls.py` placed under ENABLE + FORCE ROW LEVEL SECURITY, that the
RLS policy

    USING      (user_id = current_setting('app.user_id', true)::bigint)
    WITH CHECK (user_id = current_setting('app.user_id', true)::bigint)

actually isolates one user from another when queried through the `holzi_app`
role (NOBYPASSRLS — the role RLS bites). For each table we assert:

    1. read denial   — user B SELECTs and sees []
    2. update denial — user B's UPDATE (no WHERE) touches 0 rows
    3. delete denial — user B's DELETE (no WHERE) touches 0 rows
    4. WITH CHECK    — user A cannot INSERT a row owned by user B; the table's
       own WITH CHECK rejects it with SQLSTATE 42501. For FK-child tables the
       parent is seeded owned by A first so the rejection lands on the LEAF.

Plus two non-parametrized guards:

    5. SET ROLE bypass denied — holzi_app may not escalate to the table owner
       (holzi_owner, which bypasses RLS); rejected with SQLSTATE 42501
    6. positive control       — user A *can* read its own row back, so a
       green "0 rows for B" can never silently mask "insert never happened"

Vacuous-success trap: before asserting B sees nothing, every parametrized
case first confirms (via the owner engine, which bypasses RLS) that A's row
genuinely exists. "0 rows for B" must mean "RLS denied", never "insert failed".
"""
import secrets

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from hermes.db import tx_for_user


# --- per-table insert builders ----------------------------------------------
#
# Each builder inserts ONE leaf row with `user_id = uid` into its table. For
# FK-child tables it first seeds the required parent (conversation/persona)
# with `user_id = owner_uid`, which defaults to `uid`. Builders run on a
# connection whose `app.user_id` GUC is already set (via tx_for_user).
#
# The split between `owner_uid` (parent) and `uid` (leaf) lets the WITH CHECK
# test seed a parent owned by the GUC user (so the parent insert passes) yet
# attempt the leaf insert with a foreign `user_id` — forcing the rejection onto
# the LEAF table's WITH CHECK policy rather than the parent's. For the seven
# leaf-of-users tables there is no parent, so `owner_uid` is accepted and
# ignored.
#
# Required columns are taken verbatim from src/hermes/schema.py: every NOT NULL
# column without a server default is supplied; columns with defaults are left
# to the default.


async def _insert_sessions(conn, uid, owner_uid=None):
    await conn.execute(
        text(
            "INSERT INTO sessions(user_id, token_hash, created_at) "
            "VALUES (:u, :h, 0)"
        ),
        {"u": uid, "h": f"hash_{secrets.token_hex(8)}"},
    )


async def _insert_conversations(conn, uid, owner_uid=None):
    await conn.execute(
        text(
            "INSERT INTO conversations(user_id, channel, started_at, updated_at) "
            "VALUES (:u, 'web', 0, 0)"
        ),
        {"u": uid},
    )


async def _insert_notes(conn, uid, owner_uid=None):
    await conn.execute(
        text(
            "INSERT INTO notes(user_id, key, content, updated_at) "
            "VALUES (:u, :k, 'body', 0)"
        ),
        {"u": uid, "k": f"key_{secrets.token_hex(4)}"},
    )


async def _insert_agent_tasks(conn, uid, owner_uid=None):
    await conn.execute(
        text(
            "INSERT INTO agent_tasks(user_id, title, prompt, created_at, updated_at) "
            "VALUES (:u, 'title', 'prompt', 0, 0)"
        ),
        {"u": uid},
    )


async def _insert_personas(conn, uid, owner_uid=None):
    await conn.execute(
        text(
            "INSERT INTO personas(user_id, name, created_at, updated_at) "
            "VALUES (:u, :n, 0, 0)"
        ),
        {"u": uid, "n": f"persona_{secrets.token_hex(4)}"},
    )


async def _insert_llm_credentials(conn, uid, owner_uid=None):
    await conn.execute(
        text(
            "INSERT INTO llm_credentials"
            "(user_id, provider, mode, display_name, created_at, updated_at) "
            "VALUES (:u, 'anthropic', 'api_key', 'cred', 0, 0)"
        ),
        {"u": uid},
    )


async def _insert_tool_approvals(conn, uid, owner_uid=None):
    # Composite PK (user_id, tool_name), no auto id.
    await conn.execute(
        text(
            "INSERT INTO tool_approvals(user_id, tool_name, granted_at) "
            "VALUES (:u, :t, 0)"
        ),
        {"u": uid, "t": f"tool_{secrets.token_hex(4)}"},
    )


async def _seed_conversation(conn, uid):
    """Insert a conversation owned by `uid` and return its id (FK parent)."""
    row = (await conn.execute(
        text(
            "INSERT INTO conversations(user_id, channel, started_at, updated_at) "
            "VALUES (:u, 'web', 0, 0) RETURNING id"
        ),
        {"u": uid},
    )).first()
    return row.id


async def _seed_persona(conn, uid):
    """Insert a persona owned by `uid` and return its id (FK parent)."""
    row = (await conn.execute(
        text(
            "INSERT INTO personas(user_id, name, created_at, updated_at) "
            "VALUES (:u, :n, 0, 0) RETURNING id"
        ),
        {"u": uid, "n": f"persona_{secrets.token_hex(4)}"},
    )).first()
    return row.id


async def _insert_messages(conn, uid, owner_uid=None):
    cid = await _seed_conversation(conn, owner_uid if owner_uid is not None else uid)
    await conn.execute(
        text(
            "INSERT INTO messages(conversation_id, user_id, role, content, ts) "
            "VALUES (:c, :u, 'user', 'hi', 0)"
        ),
        {"c": cid, "u": uid},
    )


async def _insert_attachments(conn, uid, owner_uid=None):
    cid = await _seed_conversation(conn, owner_uid if owner_uid is not None else uid)
    await conn.execute(
        text(
            "INSERT INTO attachments"
            "(conversation_id, user_id, filename, content_type, size, "
            " storage_path, created_at) "
            "VALUES (:c, :u, 'f.txt', 'text/plain', 1, 'tok', 0)"
        ),
        {"c": cid, "u": uid},
    )


async def _insert_agent_runs(conn, uid, owner_uid=None):
    cid = await _seed_conversation(conn, owner_uid if owner_uid is not None else uid)
    # id is a TEXT PK — must be supplied explicitly.
    await conn.execute(
        text(
            "INSERT INTO agent_runs"
            "(id, conversation_id, user_id, channel, model, started_at, status) "
            "VALUES (:rid, :c, :u, 'web', 'm', 0, 'running')"
        ),
        {"rid": f"run_{secrets.token_hex(8)}", "c": cid, "u": uid},
    )


async def _insert_persona_history(conn, uid, owner_uid=None):
    pid = await _seed_persona(conn, owner_uid if owner_uid is not None else uid)
    await conn.execute(
        text(
            "INSERT INTO persona_history"
            "(persona_id, user_id, snapshot_json, created_at) "
            "VALUES (:p, :u, '{}', 0)"
        ),
        {"p": pid, "u": uid},
    )


class TableSpec:
    """One personal table under RLS: how to insert an owned row + which column
    the UPDATE-denial test mutates."""

    def __init__(self, table, insert_owned, updatable_col):
        self.table = table
        self.insert_owned = insert_owned
        self.updatable_col = updatable_col

    def __repr__(self):
        return self.table


SPECS = [
    TableSpec("sessions", _insert_sessions, "last_used_at"),
    TableSpec("conversations", _insert_conversations, "updated_at"),
    TableSpec("messages", _insert_messages, "ts"),
    TableSpec("attachments", _insert_attachments, "created_at"),
    TableSpec("agent_runs", _insert_agent_runs, "started_at"),
    TableSpec("notes", _insert_notes, "updated_at"),
    TableSpec("agent_tasks", _insert_agent_tasks, "updated_at"),
    TableSpec("personas", _insert_personas, "updated_at"),
    TableSpec("persona_history", _insert_persona_history, "created_at"),
    TableSpec("tool_approvals", _insert_tool_approvals, "granted_at"),
    TableSpec("llm_credentials", _insert_llm_credentials, "updated_at"),
]

assert len(SPECS) == 11, "all 11 personal tables must be covered"


# --- user seeding ------------------------------------------------------------


async def _seed_users(owner_engine):
    """Insert two `member` users via the owner engine (bypasses RLS) and
    return their ids (a, b)."""
    async with owner_engine.begin() as conn:
        a = (await conn.execute(
            text(
                "INSERT INTO users(email, role, bootstrap_completed, created_at) "
                "VALUES (:e, 'member', false, 0) RETURNING id"
            ),
            {"e": f"a_{secrets.token_hex(4)}@test.local"},
        )).first().id
        b = (await conn.execute(
            text(
                "INSERT INTO users(email, role, bootstrap_completed, created_at) "
                "VALUES (:e, 'member', false, 0) RETURNING id"
            ),
            {"e": f"b_{secrets.token_hex(4)}@test.local"},
        )).first().id
    return a, b


async def _owner_count(owner_engine, table):
    """Total rows in `table` as seen by the owner (bypasses RLS)."""
    async with owner_engine.begin() as conn:
        return (await conn.execute(
            text(f"SELECT count(*) FROM {table}")
        )).scalar()


# --- parametrized denial tests ----------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.table)
async def test_read_denial(engine, owner_engine, spec):
    """User A inserts a row; user B SELECTs and sees nothing."""
    a, b = await _seed_users(owner_engine)

    async with tx_for_user(engine, user_id=a) as conn:
        await spec.insert_owned(conn, a)

    # Vacuous-success guard: the row really exists (owner bypasses RLS).
    assert await _owner_count(owner_engine, spec.table) == 1, (
        f"{spec.table}: A's insert did not land — test would be vacuous"
    )

    async with tx_for_user(engine, user_id=b) as conn:
        rows = (await conn.execute(text(f"SELECT * FROM {spec.table}"))).all()
    assert rows == [], f"{spec.table}: user B could read user A's row (RLS hole)"


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.table)
async def test_update_denial(engine, owner_engine, spec):
    """User B's blanket UPDATE touches 0 of user A's rows."""
    a, b = await _seed_users(owner_engine)

    async with tx_for_user(engine, user_id=a) as conn:
        await spec.insert_owned(conn, a)
    assert await _owner_count(owner_engine, spec.table) == 1

    async with tx_for_user(engine, user_id=b) as conn:
        result = await conn.execute(
            text(f"UPDATE {spec.table} SET {spec.updatable_col} = 999")
        )
    assert result.rowcount == 0, (
        f"{spec.table}: user B updated {result.rowcount} of user A's rows (RLS hole)"
    )


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.table)
async def test_delete_denial(engine, owner_engine, spec):
    """User B's blanket DELETE touches 0 of user A's rows."""
    a, b = await _seed_users(owner_engine)

    async with tx_for_user(engine, user_id=a) as conn:
        await spec.insert_owned(conn, a)
    assert await _owner_count(owner_engine, spec.table) == 1

    async with tx_for_user(engine, user_id=b) as conn:
        result = await conn.execute(text(f"DELETE FROM {spec.table}"))
    assert result.rowcount == 0, (
        f"{spec.table}: user B deleted {result.rowcount} of user A's rows (RLS hole)"
    )
    # The row must still be there.
    assert await _owner_count(owner_engine, spec.table) == 1


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.table)
async def test_with_check_write_denial(engine, owner_engine, spec):
    """User A cannot plant a row owned by user B — the LEAF table's WITH CHECK
    rejects it with SQLSTATE 42501 (insufficient_privilege).

    The builder runs with A's GUC. For FK-child tables it first seeds the parent
    owned by A (`owner_uid=a`, which passes WITH CHECK since a==a), then attempts
    the leaf INSERT with `user_id = b`; the rejection therefore lands on the leaf
    table's own WITH CHECK, not the parent's. For leaf-of-users tables there is
    no parent and the single INSERT with `user_id = b` is rejected directly.
    """
    a, b = await _seed_users(owner_engine)

    with pytest.raises(DBAPIError) as exc_info:
        async with tx_for_user(engine, user_id=a) as conn:
            # Parent (if any) owned by A passes; leaf row user_id=b is rejected.
            await spec.insert_owned(conn, b, owner_uid=a)
    assert exc_info.value.orig.sqlstate == "42501", (
        f"{spec.table}: expected RLS WITH CHECK rejection (42501), "
        f"got {exc_info.value.orig.sqlstate}"
    )

    # Nothing owned by B may have been planted.
    assert await _owner_count(owner_engine, spec.table) == 0, (
        f"{spec.table}: user A planted a row owned by user B (WITH CHECK hole)"
    )


# --- non-parametrized guards -------------------------------------------------


async def test_set_role_bypass_denied(engine):
    """holzi_app (NOSUPERUSER, not a member of holzi_owner) must not escalate via
    SET ROLE to the table owner — that owner bypasses RLS (USING/WITH CHECK do
    not apply to the table owner), so a successful SET ROLE would sidestep RLS
    entirely. The attempt is rejected with SQLSTATE 42501 (insufficient_privilege).

    Target holzi_owner, the role that actually exists and owns the tables: a
    bare `SET ROLE postgres` would false-green here because no `postgres` role
    exists in the test cluster (it errors 22023 "role does not exist", not a
    privilege check).
    """
    with pytest.raises(DBAPIError) as exc_info:
        async with engine.begin() as conn:
            await conn.execute(text("SET ROLE holzi_owner"))
    assert exc_info.value.orig.sqlstate == "42501", (
        f"expected SET ROLE rejection (42501), got {exc_info.value.orig.sqlstate}"
    )


async def test_positive_control_owner_reads_own_row(engine, owner_engine):
    """Sanity: user A *can* read its own row back through the RLS-bound engine.

    This proves the green "B sees 0 rows" results above are not an artefact of
    inserts silently failing — the legitimate owner path returns the row.
    """
    a, _b = await _seed_users(owner_engine)

    async with tx_for_user(engine, user_id=a) as conn:
        await conn.execute(
            text(
                "INSERT INTO notes(user_id, key, content, updated_at) "
                "VALUES (:u, 'k', 'body', 0)"
            ),
            {"u": a},
        )

    async with tx_for_user(engine, user_id=a) as conn:
        rows = (await conn.execute(text("SELECT * FROM notes"))).all()
    assert len(rows) == 1, "owner could not read its own row — RLS over-blocks"
