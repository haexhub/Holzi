# Wave C1 — Account-Layer + User-Scoped DB Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development) to implement this plan
> task-by-task.

**Goal:** Turn the implicit single-user backend into a real account layer:
the bearer token resolves to a `(user_id, role)` via a pluggable
`IdentityResolver`, the `users` table carries identity columns, and the four
personal-data tables (`conversations`, `notes`, `agent_tasks`, `personas`)
are scoped to the authenticated user — without breaking the existing
single-token deployment.

**Architecture:** Bearer transport stays; a new `src/hermes/identity.py`
maps tokens to identities (DB lookup by `sha256(token)`), the auth
middleware sets `request.state.user_id`, and each personal-data repo gains a
required `user_id` argument that is filtered in SQL. Migrations are additive
`ALTER TABLE ADD COLUMN` backfilled to `user_id=1`. See the design doc
`2026-06-11-wave-c1-multi-user-design.md`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy Core + aiosqlite, pytest
(`asyncio_mode=auto`), ruff, mypy-strict, `uv`. Frontend touchpoint: Nuxt 4
layer in the monorepo (`~/Projekte/holzi/packages/holzi-ui`).

**Commands:** `uv run pytest tests/<file>.py -v` · `uv run ruff check src
tests` · `uv run mypy src` · `make token` · `make dev`.

**Conventions to match (verified):**
- Repo functions are module-level `async def f(engine: AsyncEngine, ...)`.
- The `conn` fixture (`tests/conftest.py:25`) yields an `AsyncEngine` on a
  fresh SQLite file. Use it for repo tests. No `@pytest.mark.asyncio` needed
  (`asyncio_mode=auto`), but existing tests add it — match the file you edit.
- Route tests: `TestClient(app)` + `Authorization: Bearer test-token-for-pytest`.
- Migrations: add to `_apply_lightweight_migrations` (`src/hermes/db.py:90`),
  guard with `PRAGMA table_info`. Mirror new columns in `schema.py` for
  fresh DBs (FK lives in `schema.py` only — see design doc SQLite caveat).
- Commit after every green task. English, concise messages.

---

## Task 1: Token hashing + `IdentityResolver` (`src/hermes/identity.py`)

**Files:**
- Create: `src/hermes/identity.py`
- Test: `tests/test_identity.py`

**Step 1: Write the failing test**

```python
# tests/test_identity.py
from sqlalchemy import text

from hermes.identity import SessionResolver, Identity, hash_token


def test_hash_token_is_stable_sha256_hex() -> None:
    h = hash_token("abc")
    assert h == hash_token("abc")
    assert len(h) == 64 and h != "abc"


async def test_resolver_returns_identity_for_active_session(conn) -> None:
    token = "secret-token-xyz"
    async with conn.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, role, bootstrap_completed, created_at) "
                 "VALUES (7, 'member', 0, 0)")
        )
        await db.execute(
            text("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) "
                 "VALUES (7, :h, 0, NULL)"),
            {"h": hash_token(token)},
        )
    ident = await SessionResolver(conn).resolve(token)
    assert ident == Identity(user_id=7, role="member")


async def test_resolver_rejects_expired_session(conn) -> None:
    token = "expired-xyz"
    async with conn.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, role, bootstrap_completed, created_at) "
                 "VALUES (8, 'member', 0, 0)")
        )
        await db.execute(
            text("INSERT INTO sessions(user_id, token_hash, created_at, expires_at) "
                 "VALUES (8, :h, 0, 1)"),   # expired at epoch 1
            {"h": hash_token(token)},
        )
    assert await SessionResolver(conn).resolve(token) is None


async def test_resolver_returns_none_for_unknown_token(conn) -> None:
    assert await SessionResolver(conn).resolve("nope") is None
```

**Step 2: Run — expect failure** (`ModuleNotFoundError: hermes.identity`)

Run: `uv run pytest tests/test_identity.py -v`

> Note: the DB-touching tests insert into `sessions` + the `role` column,
> both of which **Task 2** creates. They will fail to insert until Task 2's
> schema lands. Recommended: implement `identity.py` here (Step 3) and mark
> those three tests
> `@pytest.mark.xfail(reason="needs sessions table from Task 2")`,
> removing the marker at the end of Task 2.

**Step 3: Implement**

```python
# src/hermes/identity.py
"""Identity resolution seam (Wave C1, Plan 35 §C1).

The per-request bearer is a SESSION token. SessionResolver maps it to an
Identity via the sessions table, honouring expiry. How a session is *minted*
(email magic-link in C2, DID in haex-vault later) is a separate login
strategy — this resolver is unaffected by it.
"""
import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.schema import sessions, users


def hash_token(credential: str) -> str:
    """SHA-256 hex of a bearer/session token. High-entropy random tokens, so
    a plain digest keeps live tokens out of a DB dump."""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: int
    role: str


class IdentityResolver(Protocol):
    async def resolve(self, credential: str) -> Identity | None: ...


class SessionResolver:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def resolve(self, credential: str) -> Identity | None:
        token_hash = hash_token(credential)
        now = int(time.time())
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(users.c.id, users.c.role)
                    .select_from(
                        sessions.join(users, sessions.c.user_id == users.c.id)
                    )
                    .where(sessions.c.token_hash == token_hash)
                    .where(
                        (sessions.c.expires_at.is_(None))
                        | (sessions.c.expires_at > now)
                    )
                )
            ).first()
        return Identity(user_id=row.id, role=row.role) if row else None
```

**Step 4: Run — `hash_token` test passes; DB tests xfail (until Task 3).**

**Step 5: Commit**

```bash
git add src/hermes/identity.py tests/test_identity.py
git commit -m "feat(auth): identity resolver seam + token hashing (Wave C1)"
```

---

## Task 2: Extend `users` + add `sessions` table

**Files:**
- Modify: `src/hermes/schema.py` (the `users` Table ~line 502; add a new
  `sessions` Table next to it)
- Modify: `src/hermes/db.py` (`_apply_lightweight_migrations`, ~line 160)
- Test: `tests/test_schema_migrations.py` (new)

**Step 1: Write the failing test**

```python
# tests/test_schema_migrations.py
from sqlalchemy import text


async def test_users_has_identity_columns(conn) -> None:
    async with conn.connect() as db:
        cols = {r[1] for r in (await db.execute(text("PRAGMA table_info(users)"))).all()}
    assert {"email", "role", "parent_user_id"} <= cols


async def test_sessions_table_exists(conn) -> None:
    async with conn.connect() as db:
        cols = {r[1] for r in
                (await db.execute(text("PRAGMA table_info(sessions)"))).all()}
    assert {"id", "user_id", "token_hash", "label",
            "created_at", "last_used_at", "expires_at"} <= cols


async def test_users_role_defaults_member_on_fresh_insert(conn) -> None:
    async with conn.begin() as db:
        await db.execute(
            text("INSERT INTO users(id, bootstrap_completed, created_at) "
                 "VALUES (2, 0, 0)")
        )
    async with conn.connect() as db:
        role = (await db.execute(text("SELECT role FROM users WHERE id=2"))).scalar()
    assert role == "member"
```

**Step 2: Run — expect failure** (`no such column: email` /
`no such table: sessions`).
Run: `uv run pytest tests/test_schema_migrations.py -v`

**Step 3: Implement**

In `schema.py`, extend `users` (note: **no** token column — sessions get
their own table) and add `sessions`:
```python
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", Text, unique=True),
    Column("role", Text, nullable=False, server_default="member"),
    Column("parent_user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column("bootstrap_completed", Integer, nullable=False, server_default="0"),
    Column("created_at", Integer, nullable=False),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer,
           ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", Text, nullable=False, unique=True),  # sha256 of the bearer
    Column("label", Text),                       # user-agent / "VS Code" / "bootstrap admin"
    Column("created_at", Integer, nullable=False),
    Column("last_used_at", Integer),
    Column("expires_at", Integer),               # NULL = never (bootstrap / long-lived)
)
```

In `db.py` `_apply_lightweight_migrations`, append guarded blocks. The new
`sessions` table is created automatically by `metadata.create_all` on every
DB (fresh + existing) — only the `users` columns need an `ALTER`, and only
the per-user lookup index needs an explicit create (`token_hash` is already
covered by its UNIQUE index):
```python
cols = await conn.execute(text("PRAGMA table_info(users)"))
existing = {row[1] for row in cols.all()}
if "email" not in existing:
    await conn.execute(text("ALTER TABLE users ADD COLUMN email TEXT"))
if "role" not in existing:
    await conn.execute(
        text("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
    )
    await conn.execute(text("UPDATE users SET role = 'admin' WHERE id = 1"))
if "parent_user_id" not in existing:
    await conn.execute(text("ALTER TABLE users ADD COLUMN parent_user_id INTEGER"))

await conn.execute(text(
    "CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)"))
```

**Step 4: Run — expect PASS.** Then remove the `xfail` markers added in
Task 1 and run `uv run pytest tests/test_identity.py tests/test_schema_migrations.py -v` — all green.

**Step 5: Commit**

```bash
git add src/hermes/schema.py src/hermes/db.py tests/test_schema_migrations.py tests/test_identity.py
git commit -m "feat(db): users identity columns + sessions table (Wave C1)"
```

---

## Task 3: Admin bootstrap session from the static token (`src/hermes/users.py`)

**Files:**
- Modify: `src/hermes/users.py` (`ensure_users_seeded`)
- Test: extend `tests/test_users_repo.py`

**Step 1: Write the failing test**

```python
# tests/test_users_repo.py  (append)
from sqlalchemy import text

from hermes.identity import hash_token
from hermes.config import settings


async def test_seed_creates_admin_bootstrap_session(conn) -> None:
    await ensure_users_seeded(conn)
    async with conn.connect() as db:
        role = (await db.execute(text("SELECT role FROM users WHERE id=1"))).scalar()
        sess = (await db.execute(text(
            "SELECT user_id, token_hash, expires_at FROM sessions"
        ))).first()
    assert role == "admin"
    assert sess.user_id == 1
    assert sess.token_hash == hash_token(settings.auth_token)
    assert sess.expires_at is None   # never expires (operator's own machine)


async def test_seed_is_idempotent_no_duplicate_session(conn) -> None:
    await ensure_users_seeded(conn)
    await ensure_users_seeded(conn)  # second boot
    async with conn.connect() as db:
        count = (await db.execute(text("SELECT COUNT(*) FROM sessions"))).scalar()
    assert count == 1
```

**Step 2: Run — expect failure** (no `sessions` row yet).
Run: `uv run pytest tests/test_users_repo.py -v`

**Step 3: Implement** — extend `ensure_users_seeded`:

```python
async def ensure_users_seeded(engine: AsyncEngine) -> None:
    from hermes.config import settings
    from hermes.identity import hash_token

    now = int(time.time())
    token_hash = hash_token(settings.auth_token)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO users(id, role, bootstrap_completed, created_at) "
                "VALUES (1, 'admin', 0, :now)"
            ),
            {"now": now},
        )
        # Turn the static HERMES_AUTH_TOKEN into a never-expiring admin session
        # so the existing deployment keeps working. The UNIQUE `token_hash`
        # makes INSERT OR IGNORE a no-op on re-seed. expires_at = NULL.
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO sessions"
                "(user_id, token_hash, label, created_at, expires_at) "
                "VALUES (1, :h, 'bootstrap admin', :now, NULL)"
            ),
            {"h": token_hash, "now": now},
        )
```

**Step 4: Run — expect PASS.**

**Step 5: Commit**

```bash
git add src/hermes/users.py tests/test_users_repo.py
git commit -m "feat(auth): bootstrap admin session from HERMES_AUTH_TOKEN (Wave C1)"
```

---

## Task 4: Auth middleware uses the resolver (`src/hermes/auth.py`)

**Files:**
- Modify: `src/hermes/auth.py`
- Modify: `src/hermes/main.py` (lifespan: build `app.state.identity_resolver`)
- Test: extend `tests/test_auth.py`

**Step 1: Write the failing test**

```python
# tests/test_auth.py  (append)
def test_request_state_user_id_is_set_for_valid_token() -> None:
    # /api/auth/me (Task 5) echoes request.state.user_id; until it exists,
    # assert the valid-token path returns 200 and move the user_id assertion
    # into Task 5's test.
    with TestClient(app) as client:          # `with` → lifespan boots the DB + seeds admin session
        r = client.get("/api/ping", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200


def test_unknown_token_is_401_via_resolver() -> None:
    with TestClient(app) as client:
        r = client.get("/api/ping", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
```

> **Lifespan gotcha (important):** before this refactor, auth was a pure HMAC
> compare needing no DB, so `test_auth.py` used a bare `TestClient(app)`
> (lifespan never ran). After the refactor the resolver needs
> `app.state.identity_resolver` + the seeded admin session, which only exist
> once the **lifespan runs**. So **migrate the existing four `test_auth.py`
> cases to `with TestClient(app) as client:`** too. They stay green: the
> valid-token case passes because the lifespan seeds the admin session with
> `hash_token(VALID_TOKEN)` (Task 3); the `_reset_app_db_path` autouse
> fixture (`conftest.py:40`) gives each test a fresh DB. This migration is a
> legitimate consequence of the refactor, not a behavior change.

**Step 2: Run — expect the new unknown-token case to pass already, but
verify no regression once Step 3 lands.** Run: `uv run pytest tests/test_auth.py -v`

**Step 3: Implement**

`main.py` lifespan, right after `app.state.db = await init_db(...)` and
*before* `ensure_users_seeded`:
```python
from hermes.identity import SessionResolver
app.state.identity_resolver = SessionResolver(app.state.db)
```

`auth.py` — replace the HMAC compare:
```python
from fastapi import Request, Response

async def bearer_auth_middleware(request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if not header.startswith(BEARER_PREFIX):
        return _unauthorized(request, reason="missing_or_malformed")
    provided = header[len(BEARER_PREFIX):]
    identity = await request.app.state.identity_resolver.resolve(provided)
    if identity is None:
        return _unauthorized(request, reason="invalid_or_expired_session")
    request.state.user_id = identity.user_id
    request.state.role = identity.role
    return await call_next(request)


def current_user_id(request: Request) -> int:
    return request.state.user_id


def current_role(request: Request) -> str:
    return request.state.role
```
Drop the now-unused `hmac` + `settings` imports if nothing else needs them
(`settings` is still imported elsewhere? check — only remove what *this*
change orphans).

**Step 4: Run** all auth tests — `uv run pytest tests/test_auth.py -v` — expect PASS (5 original + 2 new).

**Step 5: Commit**

```bash
git add src/hermes/auth.py src/hermes/main.py tests/test_auth.py
git commit -m "feat(auth): resolve bearer to user via IdentityResolver (Wave C1)"
```

---

## Task 5: `GET /api/auth/me` + `POST /api/auth/logout`

**Files:**
- Create: `src/hermes/routes/auth.py` (small, cohesive)
- Modify: `src/hermes/main.py` (`include_router(auth_router)`)
- Test: `tests/test_api_auth.py` (new)

**Step 1: Write the failing test**

```python
# tests/test_api_auth.py
from fastapi.testclient import TestClient
from hermes.main import app

VALID_TOKEN = "test-token-for-pytest"
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


def test_auth_me_returns_admin_identity() -> None:
    with TestClient(app) as client:
        r = client.get("/api/auth/me", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == 1
    assert body["role"] == "admin"
    assert "bootstrap_completed" in body


def test_auth_me_requires_auth() -> None:
    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401


def test_logout_invalidates_session() -> None:
    with TestClient(app) as client:
        assert client.post("/api/auth/logout", headers=AUTH).status_code == 200
        # session row deleted → the same token no longer resolves
        assert client.get("/api/auth/me", headers=AUTH).status_code == 401
```

**Step 2: Run — expect 404 (route missing).** Run: `uv run pytest tests/test_api_auth.py -v`

**Step 3: Implement** `routes/auth.py`:

```python
from fastapi import APIRouter, Request
from sqlalchemy import select

from hermes.auth import current_role, current_user_id
from hermes.identity import hash_token
from hermes.schema import sessions, users

router = APIRouter(prefix="/api/auth")

_BEARER = "Bearer "


@router.get("/me")
async def me(request: Request) -> dict:
    uid = current_user_id(request)
    async with request.app.state.db.connect() as conn:
        row = (await conn.execute(
            select(users.c.email, users.c.bootstrap_completed).where(users.c.id == uid)
        )).first()
    return {
        "user_id": uid,
        "role": current_role(request),
        "email": row.email if row else None,
        "bootstrap_completed": bool(row.bootstrap_completed) if row else False,
    }


@router.post("/logout")
async def logout(request: Request) -> dict:
    """Delete the session backing the presented bearer. Idempotent."""
    header = request.headers.get("authorization", "")
    token = header[len(_BEARER):] if header.startswith(_BEARER) else ""
    async with request.app.state.db.begin() as conn:
        await conn.execute(
            sessions.delete().where(sessions.c.token_hash == hash_token(token))
        )
    return {"ok": True}
```
Register in `main.py`: `from hermes.routes.auth import router as auth_router`
then `app.include_router(auth_router)`.

> Note: logging out the bootstrap admin session deletes the only session in
> a fresh test DB — fine, because each test gets its own DB (autouse
> `_reset_app_db_path`). In production the operator can re-mint by restarting
> (re-seeds) or, post-C2, via the magic-link flow.

**Step 4: Run — expect PASS.**

**Step 5: Commit**

```bash
git add src/hermes/routes/auth.py src/hermes/main.py tests/test_api_auth.py
git commit -m "feat(api): /api/auth/me + /api/auth/logout (Wave C1)"
```

---

## Task 6: Scope `conversations` by `user_id`

**Files:**
- Modify: `src/hermes/schema.py` (`conversations` Table) + `src/hermes/db.py`
  (migration + index)
- Modify: `src/hermes/repository/conversations.py`
- Modify: `src/hermes/repository/models.py` (`Conversation` dataclass: add
  `user_id: int`)
- Modify: callers — `routes/api.py` (~248-266, 1136-1363),
  `routes/chat.py:53-66`, `routes/ws_agent.py:117`
- Test: extend `tests/test_conversations.py` + `tests/test_api_conversations.py`

**Step 1: Write the failing test** (repo isolation)

```python
# tests/test_conversations.py  (append)
from hermes.repository import conversations as repo


async def test_get_is_scoped_to_owner(conn) -> None:
    mine = await repo.create(conn, user_id=1, channel="web", title="mine")
    # another user's row is invisible to user 1
    other = await repo.create(conn, user_id=2, channel="web", title="theirs")
    assert await repo.get(conn, mine.id, user_id=1) is not None
    assert await repo.get(conn, other.id, user_id=1) is None


async def test_list_all_filters_by_user(conn) -> None:
    await repo.create(conn, user_id=1, channel="web", title="a")
    await repo.create(conn, user_id=2, channel="web", title="b")
    rows = await repo.list_all(conn, user_id=1, channel="web")
    assert [c.title for c in rows] == ["a"]
```

**Step 2: Run — expect failure** (`create() got an unexpected keyword
argument 'user_id'`). Run: `uv run pytest tests/test_conversations.py -v`

**Step 3: Implement**

- `schema.py`: add to `conversations` Table:
  `Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, server_default="1")`
- `db.py` migration (guarded by `PRAGMA table_info(conversations)`):
  ```python
  if "user_id" not in existing:
      await conn.execute(text(
          "ALTER TABLE conversations ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"))
  await conn.execute(text(
      "CREATE INDEX IF NOT EXISTS conv_user_updated "
      "ON conversations(user_id, updated_at DESC)"))
  ```
- `models.py`: add `user_id: int` to `Conversation`; update
  `_row_to_conversation` and the `create` return in `conversations.py`.
- `conversations.py`: thread `user_id`:
  - `create(engine, *, user_id, channel, ...)` → `.values(user_id=user_id, ...)`
  - `get(engine, conversation_id, *, user_id)` → add `.where(c.user_id == user_id)`
  - `list_by_channel` / `list_all` / `find_latest_by_external_id` → add
    `.where(c.user_id == user_id)`
  - `update_title` / `set_bookmarked` / `touch` / `delete` /
    `message_count` → add `AND user_id = :user_id` to their `WHERE`
  - `search` → add `c.user_id = :user_id` to the SQL WHERE; bind param
  - `list_expired` / `sweep_expired` are **global by design** (the sweeper
    is not user-scoped in C1) — leave them. Note this in a code comment.
- Callers: pass `user_id=current_user_id(request)`. For `ws_agent.py`,
  resolve identity (Task 4 pattern) and pass `user_id`. For the scheduler
  path that creates task conversations, pass the `agent_task`'s `user_id`
  (Task 8 adds it; until then pass `1`).

**Step 4: Run** repo + route tests — `uv run pytest tests/test_conversations.py tests/test_api_conversations.py -v` — expect PASS. Then `uv run mypy src` (catches any missed call site).

**Step 5: Commit**

```bash
git add -A && git commit -m "feat(db): scope conversations by user_id (Wave C1)"
```

---

## Task 7: Scope `notes` by `user_id`

**Files:** `schema.py` (`notes` + the `notes_fts` note — FTS triggers in
`schema.sql` index `key/content/tags`; `user_id` is not searched, no trigger
change needed), `db.py` (migration + index), `repository/notes.py`,
`models.py` (`Note`), notes route(s), `tests/` (new
`tests/test_notes_repo.py` if absent, else extend).

Same shape as Task 6. Note the `notes.key` column is currently `unique` —
with multiple users the uniqueness must become **per-user**
(`UNIQUE(user_id, key)`). In `schema.py` drop the column-level `unique=True`
on `key` and add `UniqueConstraint("user_id", "key")`. Migration: SQLite
can't drop a UNIQUE easily; document that fresh DBs get the composite
constraint and the single existing user is unaffected (one user → no
collision). Memory writes (`memory_search` / notes tool) pass the agent's
`user_id`.

TDD steps mirror Task 6 (failing isolation test → schema+migration →
thread `user_id` → green → commit).

```bash
git commit -m "feat(db): scope notes by user_id (Wave C1)"
```

---

## Task 8: Scope `agent_tasks` by `user_id`

**Files:** `schema.py` (`agent_tasks`), `db.py` (migration + index on
`(user_id, enabled, due_at)`), `repository/agent_tasks.py`, `models.py`
(`AgentTask`), `routes/` (tasks endpoints), `src/hermes/scheduler.py`,
`tests/test_agent_tasks_repo.py` (extend).

Extra: the **scheduler** (`scheduler.py`) calls `list_due` then creates a
conversation per fired task. `list_due` stays global (the scheduler serves
all users), but each fired task now carries `user_id`, which is passed to
`conversations.create(..., user_id=task.user_id)`. Add a test that a task
seeded for `user_id=2` produces a conversation owned by user 2.

TDD steps mirror Task 6.

```bash
git commit -m "feat(db): scope agent_tasks by user_id + scheduler (Wave C1)"
```

---

## Task 9: Scope `personas` by `user_id` + per-user default

**Files:** `schema.py` (`personas`: add `user_id`; the single-default
trigger in `schema.sql` lines ~111-123 must become **per-user** — "one
default *per user*"), `db.py` (migration + index), `repository/personas.py`,
`models.py` (`Persona`), `routes/preferences.py` (~201) and any persona
routes, `src/hermes/personas.py` (`ensure_personas_backfill`,
`resolve_persona_context`, `get_default`), `tests/test_personas_repo.py`.

Key subtlety — the **single-default trigger**: today it enforces one
`is_default=1` row globally. Rewrite it to enforce one default *per
`user_id`* (the `WHERE` in the trigger gains `AND user_id = NEW.user_id`).
This is a `schema.sql` change applied idempotently; on existing DBs drop +
recreate the trigger inside `_apply_lightweight_migrations` (triggers are
cheap to `DROP TRIGGER IF EXISTS` + recreate).

`resolve_persona_context` / `get_default` / `get_effective_system_prompt`
gain a `user_id` argument so the agent composes *the calling user's* default
persona. The autouse test fixture in `conftest.py:67`
(`_patch_persona_context_for_app_tests`) patches `resolve_persona_context` —
update its fake signature to accept `user_id` so the suite keeps compiling.

`ensure_personas_backfill` seeds the default persona for `user_id=1`.

TDD: failing test "two users each get their own default persona" → schema +
trigger + threading → green.

```bash
git commit -m "feat(db): scope personas by user_id + per-user default (Wave C1)"
```

---

## Task 10: Frontend — surface identity (monorepo `~/Projekte/holzi/`)

> **Different repo.** This task is in the Nuxt monorepo, not the Hermes
> backend. Its tests run with `pnpm test` / vitest there.

**Files:**
- Modify: `packages/holzi-ui/stores/auth.ts`
- Possibly add a composable call where login completes (search for
  `setToken` usage / the login page in `packages/holzi-ui/pages/login.vue`).
- Test: `apps/frontend/tests/stores/auth.test.ts` (extend — it exists).

**Goal:** after a session token is set, fetch `GET /api/auth/me`; store
`role` + `userId`; expose `isAdmin` computed. Add a `logout()` that calls
`POST /api/auth/logout` then clears local state. On 401, clear the token.
This is what C2 uses to gate the `/settings/family` admin UI. **Scope note:**
the ephemeral / "stay signed in" UX (sessionStorage vs localStorage) and the
magic-link login page land in **C2** — in C1 the operator still pastes the
bootstrap token; this task only surfaces identity + wires logout.

**Step 1:** failing vitest — store exposes `role`/`isAdmin`, `setToken`
triggers a `/api/auth/me` fetch, and `logout()` calls `/api/auth/logout` +
clears (mock the fetch).
**Step 2:** run `pnpm --filter @holzi/... test` (match the repo's script) — fail.
**Step 3:** implement: add `role`/`userId` refs, async `loadIdentity()` +
`logout()` calling the existing API composable, `isAdmin = computed(() =>
role.value === 'admin')`.
**Step 4:** `pnpm test` + `pnpm typecheck` green.
**Step 5:** commit in the monorepo:
```bash
git commit -m "feat(holzi-ui): load + expose auth identity (Wave C1)"
```

---

## Task 11: Integration sweep + verification

**No new code unless a gap is found.**

1. `uv run pytest` (full backend suite, `-m 'not integration'`) — all green.
2. `uv run ruff check src tests` — clean.
3. `uv run mypy src` — clean (this is the real cross-user-leak guard: every
   repo call site must now pass `user_id`).
4. Manual smoke (`make dev`, fresh DB):
   - `curl -H "Authorization: Bearer $HERMES_AUTH_TOKEN" localhost:8082/api/auth/me`
     → `{"user_id":1,"role":"admin",...}`.
   - A wrong token → 401.
   - Create a conversation, list conversations → it appears; the DB row has
     `user_id=1`.
   - `/ws/agent` with the token connects; its conversation has `user_id=1`.
5. Monorepo: `pnpm typecheck` + `pnpm test` green; manual login still works.
6. Update the roadmap (`~/Projekte/holzi/docs/plans/35-strategic-roadmap-2026h2.md`):
   under Wave C add "**C1 completed YYYY-MM-DD**" per the wave-end checklist.

**Commit (if any sweep fixes were needed):**
```bash
git commit -m "chore(c1): integration sweep — full suite + mypy green (Wave C1)"
```

---

## Task ordering note

Tasks 1→5 are the auth/identity foundation and are independent of the
table-scoping tasks. Tasks 6→9 each follow the identical
schema+migration+repo+routes+test shape and can be done in any order (or
dispatched in parallel to subagents) once Tasks 1-5 land. Task 4 must
precede 6-9's *route* edits (routes need `current_user_id`). Task 10 (FE)
needs Task 5 (`/api/auth/me`) deployed/available. Task 11 is last.
