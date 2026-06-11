# Design: Wave C1 — Account-Layer + User-Scoped DB

> **STATUS: SHIPPED 2026-06-11** — merged to `main` (backend PR #85, plans
> PR #84; frontend identity+logout via holzi-monorepo PR #3). Backend CI
> green (ruff/mypy/pytest, 1041 tests). Built as designed below. CodeRabbit
> review on #85: two single-admin bugs fixed (logout no longer revokes the
> env bootstrap session; `HERMES_AUTH_TOKEN` rotation drops the stale
> bootstrap session); its remaining findings were multi-user concerns
> deferred to C2/C3 (see the updated C2/C3 outlines + #85's disposition
> comment).

> **For Claude:** DESIGN DOC. Resolves the C1 open questions from Plan 35
> (`docs/plans/35-strategic-roadmap-2026h2.md` lives in the monorepo
> `~/Projekte/holzi/`). Implementation plan:
> `2026-06-11-wave-c1-multi-user-impl.md`. Read this first.

## Summary

C1 is the **foundation** of Wave C (Multi-User / Family-Mode). It does the
structural, hard-to-reverse work and nothing else:

1. Extend the existing `users` table (`email` / `role` / `parent_user_id`)
   **via additive `ALTER TABLE ADD COLUMN`** (not a drop-and-recreate), and
   add a **`sessions`** table.
2. Make the per-request bearer a **short-lived session token**, not a
   long-lived identity secret. A pluggable **`IdentityResolver`** maps a
   session token → `(user_id, role)`. *How a session is minted* (email
   magic-link in C2, DID in a future `haex-vault`) is a separate, pluggable
   **login strategy** — the per-request path only ever looks up sessions.
3. Thread a **`user_id` owner column** through the four *personal-data*
   tables (`conversations`, `notes`, `agent_tasks`, `personas`) and scope
   every query to the authenticated user.
4. Bootstrap the existing single user (`id=1`) as the **admin**, turning the
   existing `HERMES_AUTH_TOKEN` into a **long-lived admin session** so
   nothing breaks on upgrade.

**Explicitly NOT in C1** (deferred to C2/C3 — see Non-Goals): the email
magic-link login flow + SMTP, one-time login tokens, "stay signed in",
session-management UI, creating additional users, invites, role enforcement,
resource sharing, per-user sandbox/MCP isolation, and `/settings/family`.

## Decisions (resolved with the user, 2026-06-11)

| Question | Decision | Rationale |
|---|---|---|
| Auth model (Plan 35 open question C1) | The per-request bearer is a **session token** stored hashed in a `sessions` table; the `IdentityResolver` resolves it to `(user_id, role)`. **Login is email magic-link for everyone** (C2) — no passwords. Sessions are **ephemeral by default** (sessionStorage, short TTL); "stay signed in" is opt-in on trusted devices. | A long-lived token in a foreign machine's `localStorage` (internet café) is the threat the user wants gone. A short-lived session that dies with the browser tab + a server-side TTL + explicit logout leaves nothing behind. Magic-link = the user's stated preference ("send a link by mail each time"). |
| Who logs in by email | **Everyone, uniformly** — adults, children, and the VS Code frontend all authenticate via email magic-link → session. | The user picked the uniform model. Simpler than special-casing device tokens/children. Children get an email address provided by the family admin; VS Code uses the code/deep-link variant of the magic link (see C2). |
| Shared vs per-user resources | **Per-user personal data + admin-sharing for infra.** C1 scopes only personal-data tables; the sharing model for `llm_credentials` / `skills` / `mcp_servers` / `workspaces` is designed in C2 and enforced in C3. | A `child` has no API key, so `llm_credentials` cannot be purely per-user. Adding owner/sharing columns before a second user exists is speculative (YAGNI). |
| Migration approach | Additive `ALTER TABLE ADD COLUMN` + a new `CREATE TABLE`, backfill existing rows to `user_id = 1`. No drop-and-recreate. | The `users` table already exists (Plan 37) and its schema comment (`src/hermes/schema.py:498`) mandates ALTER. Matches the established `_apply_lightweight_migrations` pattern in `src/hermes/db.py`. |

## Current state (verified against the code, do not re-derive)

- **Auth today**: a single static bearer token. `bearer_auth_middleware`
  (`src/hermes/auth.py:14-29`) does `hmac.compare_digest(provided,
  settings.auth_token)`. No user concept on the request. `/ws/agent` does
  its *own* bearer check (`src/hermes/routes/ws_agent.py:88-99`) because
  Starlette's `BaseHTTPMiddleware` does not wrap WebSockets.
- **Config**: `settings.auth_token` is required (`src/hermes/config.py:14`),
  pydantic `BaseSettings`, env prefix `HERMES_`.
- **`users` table** (`src/hermes/schema.py:502-513`): `id`,
  `bootstrap_completed`, `created_at`. Seeded with one row `id=1` by
  `ensure_users_seeded` (`src/hermes/users.py:13-25`), called from the
  lifespan (`src/hermes/main.py:158`).
- **DB**: SQLAlchemy Core + `aiosqlite`, async. Tables in `schema.py`
  (`metadata.create_all`), FTS5 in `schema.sql`. Additive migrations in
  `_apply_lightweight_migrations` (`src/hermes/db.py:90-166`), guarded by
  `PRAGMA table_info`. FK enforcement ON per-connection (`db.py:29-41`).
- **SQLite ALTER caveat (verified)**: a `NOT NULL` column added via `ALTER
  TABLE ADD COLUMN` must have a non-NULL `DEFAULT` and cannot carry an inline
  `REFERENCES`. The codebase already declares FKs only in `schema.py` (fresh
  DBs) and plain columns in the ALTER (existing DBs) — see
  `agent_runs.agent_task_id` (`db.py:160-165`). C1 follows this. New *tables*
  (`sessions`) are created by `metadata.create_all` on every DB — no ALTER.
- **Repositories** (`src/hermes/repository/*.py`): module-level async
  functions taking `engine: AsyncEngine`; no `user_id` today.
  `conversations.py` is the reference shape.
- **Routes** read the engine via `request.app.state.db` (some modules use a
  local `_db(request)` helper). No FastAPI `Depends` DI for the DB.
- **`models.py:178`** already anticipates this: *"will swap user-tier writes
  for a real user_id."*
- **Tests**: pytest, `asyncio_mode = "auto"`. `conn` fixture
  (`tests/conftest.py:25-37`) yields an `AsyncEngine` on a fresh per-test
  SQLite file. Route tests use `TestClient(app)` (see `tests/test_auth.py`).
  Token in tests: `"test-token-for-pytest"` (`conftest.py:13`).
- **Commands**: `uv run pytest` (single file: `uv run pytest tests/X.py -v`),
  `uv run ruff check src tests`, `uv run mypy src` (strict), `make token`,
  `make dev`.

## Architecture

### The two layers (key mental model)

```
LOGIN  (prove "who you are", mints a session)     SESSION  (per request, "logged in now")
┌───────────────────────────────────────┐        ┌────────────────────────────────────────┐
│ C1:  static HERMES_AUTH_TOKEN (admin)  │──mint─►│ sessions table                           │
│ C2:  email magic-link (everyone)       │──mint─►│ (user_id, token_hash, expires_at, label) │
│ later: DID via haex-vault              │──mint─►│   ← IdentityResolver looks up ONLY here  │
└───────────────────────────────────────┘        └────────────────────────────────────────┘
```

The per-request path is uniform: bearer → session lookup → `(user_id,
role)`. The *minting* side is where the pluggability lives — C1 ships only
the bootstrap minter; C2 adds the email magic-link minter; haex-vault adds a
DID minter. None of them change routes or repos.

### 1. `IdentityResolver` seam (new `src/hermes/identity.py`)

```python
@dataclass(frozen=True, slots=True)
class Identity:
    user_id: int
    role: str  # 'admin' | 'member' | 'child' (only 'admin' exists in C1)

class IdentityResolver(Protocol):
    async def resolve(self, credential: str) -> Identity | None: ...
```

Default implementation — resolves a **session token** against `sessions`
(joined to `users` for the role), honouring expiry:

```python
class SessionResolver:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def resolve(self, credential: str) -> Identity | None:
        token_hash = hash_token(credential)   # sha256 hex
        now = int(time.time())
        async with self._engine.connect() as conn:
            row = (await conn.execute(
                select(users.c.id, users.c.role)
                .select_from(sessions.join(users, sessions.c.user_id == users.c.id))
                .where(sessions.c.token_hash == token_hash)
                .where((sessions.c.expires_at.is_(None))
                       | (sessions.c.expires_at > now))
            )).first()
        return Identity(user_id=row.id, role=row.role) if row else None
```

- `hash_token` = `hashlib.sha256(credential.encode()).hexdigest()`. Session
  tokens are high-entropy random; storing only the SHA-256 keeps live
  tokens out of a DB dump. Lookup is indexed equality (`token_hash UNIQUE`),
  O(1), not timing-sensitive.
- `expires_at IS NULL` = never expires — used only for the bootstrap admin
  session (and, in C2, opt-in "stay signed in" gets a far-future expiry).
- `app.state.identity_resolver = SessionResolver(app.state.db)` is built in
  the lifespan. A future DID deployment doesn't replace this — it adds a
  *login strategy* that mints sessions; the resolver stays.

### 2. Auth middleware (`src/hermes/auth.py`)

`bearer_auth_middleware` stops comparing against `settings.auth_token` and
instead resolves the session:

```python
identity = await request.app.state.identity_resolver.resolve(provided)
if identity is None:
    return _unauthorized(request, reason="invalid_or_expired_session")
request.state.user_id = identity.user_id
request.state.role = identity.role
return await call_next(request)
```

Helpers (same module): `current_user_id(request) -> int`, `current_role(request) -> str`.
`/ws/agent` (`routes/ws_agent.py`) gets the same resolve (header or
`?token=`), close 4001 on `None`, and stashes `user_id` for the conversation
it creates.

### 3. `users` extension + `sessions` table

**`users`** gains identity columns (no token column — sessions live in their
own table). `schema.py`:
```python
Column("email", Text, unique=True),                 # nullable until the user sets it
Column("role", Text, nullable=False, server_default="member"),
Column("parent_user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
```
Migration (guarded by `PRAGMA table_info(users)`):
```sql
ALTER TABLE users ADD COLUMN email TEXT;
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member';
ALTER TABLE users ADD COLUMN parent_user_id INTEGER;
UPDATE users SET role = 'admin' WHERE id = 1;   -- existing single user is the admin
```
(`email` is nullable: the bootstrap admin has none until they set it in
onboarding; magic-link login in C2 requires it to be set. New users created
via invites in C2 always have an email from creation.)

**`sessions`** — new table, the per-request bearer store. `schema.py`:
```python
sessions = Table(
    "sessions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer,
           ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", Text, nullable=False, unique=True),  # sha256 of the bearer
    Column("label", Text),                  # user-agent / "VS Code" / "bootstrap admin"
    Column("created_at", Integer, nullable=False),
    Column("last_used_at", Integer),
    Column("expires_at", Integer),          # NULL = never; short TTL for ephemeral logins
)
```
Created by `metadata.create_all` on every DB; no ALTER needed. Add
`CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)` in the
migration (the `token_hash` lookup is already covered by its UNIQUE index).

### 4. Admin bootstrap (`src/hermes/users.py`)

`ensure_users_seeded` is extended: after inserting/locating user 1 (role
`admin`), ensure a `sessions` row exists with `token_hash =
hash_token(settings.auth_token)`, `expires_at = NULL`, `label = 'bootstrap
admin'`, `user_id = 1` (insert only if absent — `INSERT OR IGNORE` on the
UNIQUE `token_hash` makes re-seed a no-op). This turns the operator's
existing static token into a **long-lived admin session**, so on upgrade the
current token keeps working and resolves to `user_id=1, role=admin`.
`settings.auth_token` becomes the *admin bootstrap token* — usable from the
operator's own machine; everyone else logs in by email (C2).

### 5. `user_id` on personal-data tables

Add `user_id INTEGER NOT NULL` (DEFAULT 1 backfill) to exactly four tables:

| Table | Direct `user_id`? | Why |
|---|---|---|
| `conversations` | **yes** | root of chat data |
| `notes` | **yes** | the per-user memory store |
| `agent_tasks` | **yes** | scheduled tasks belong to a user |
| `personas` | **yes** | each user has their own personas |
| `messages`, `attachments`, `agent_runs` | no — transitive via `conversation_id` | already cascade-scoped |
| `persona_history` | no — transitive via `persona_id` | scoped through the persona |
| `llm_credentials`, `skills`, `mcp_servers`, `workspaces`, `channel_prompts`, `tool_approvals`, `sandbox_crashes` | no — stay global in C1 | shareable/infra; ownership + sharing is C2/C3 |

Per table:
```sql
ALTER TABLE conversations ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1;
-- FK declared in schema.py for fresh DBs; DEFAULT 1 backfills existing rows
CREATE INDEX IF NOT EXISTS conv_user_updated ON conversations(user_id, updated_at DESC);
```
`DEFAULT 1` is a backfill convenience; repo `create()` always passes
`user_id` explicitly. Read/update/delete add `AND user_id = :user_id` so a
row owned by another user returns `None` → 404, never leaks cross-user.

### 6. Repository + route threading

Each of the four repos gets a required `user_id` argument on `create`,
filtered into the `WHERE` on every read/update/delete/list. Routes resolve
`user_id = current_user_id(request)` and pass it down. Call sites (verified):
`routes/api.py` (~248-266, 1136-1363), `routes/chat.py:53-66`,
`routes/ws_agent.py:117`, `routes/preferences.py` (~201), notes + agent_tasks
routes. The TTL sweeper (`conversations.list_expired/sweep_expired`) stays
**global by design** — note it in a comment.

### 7. Per-user default persona

`ensure_personas_backfill` seeds the default persona for `user_id=1`.
Provisioning a default persona for a *new* user is C2's "create user"
routine — out of scope here (only one user exists).

### 8. Frontend touchpoint (monorepo `~/Projekte/holzi/`)

C1 is almost entirely backend. In C1 the operator still pastes the bootstrap
token (their own machine) — `packages/holzi-ui/stores/auth.ts` keeps working
unchanged, but now also calls `GET /api/auth/me` to validate + expose `role`,
and gains a `logout()` that calls `POST /api/auth/logout`. **The ephemeral /
"stay signed in" UX ships in C2 alongside the magic-link flow** — that's
where the foreign-machine concern is actually solved (sessionStorage by
default, opt-in localStorage). C1 just makes the bearer a session so C2 can
build on it. One small task; flagged as **Task 10 (monorepo)**.

## Non-Goals (explicitly deferred)

- **Email magic-link login + SMTP + one-time login tokens** → C2.
- **"Stay signed in" toggle / sessionStorage-vs-localStorage UX** → C2.
- **Session-management UI** ("log out everywhere", list active sessions) → C2.
- **Creating users / invites / children onboarding** → C2.
- **Role enforcement** (`child` forced tool approval, admin gating) → C2.
- **Resource sharing model** (owner_id + shares) → C2 (schema) / C3 (runtime).
- **Per-user `app.state` isolation** (sandbox/MCP/session_approvals) → C3.
- **DID login strategy (haex-vault)** → future; adds a session minter, no
  schema/route change.
- **Passwords** → not planned (magic-link; DID later).

## New dependency introduced (in C2, flagged now)

Magic-link login requires the server to **send email** → an SMTP config
(`HERMES_SMTP_*`) or transactional-email provider. This is a new runtime
dependency for the family-box operator. Transport choice is a C2-plan-time
decision; C1 introduces nothing email-related.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Adding `NOT NULL user_id` breaks existing rows | `DEFAULT 1` backfill; guarded by `PRAGMA table_info`. |
| A query forgets the `user_id` filter → cross-user leak | Every repo fn *requires* `user_id`; route tests assert two-user isolation; mypy-strict catches missing kwargs. |
| `/ws/agent` bypasses the HTTP middleware | C1 updates `ws_agent.py` to resolve the session through the same resolver. |
| Resolver lookup cost per request | Indexed `sessions.token_hash` equality + join to `users`; one `SELECT` on a tiny table. Negligible. |
| Stale sessions accumulate | A periodic prune of `expires_at < now` (cheap) — add to the existing sweeper in C2 when TTLs become real; in C1 only the never-expiring bootstrap session exists. |
| Future DID needs request context the resolver can't see | DID is a *login strategy* that mints a session out-of-band; the per-request resolver is unaffected. |

## Success criteria (C1)

- Existing `HERMES_AUTH_TOKEN` still authenticates and resolves to
  `user_id=1, role=admin` (no operator action on upgrade).
- An invalid/expired session token → 401 (HTTP) / close 4001 (WS).
- Every `conversations` / `notes` / `agent_tasks` / `personas` read and
  write is scoped to `request.state.user_id`; a forged `user_id` cannot read
  another user's rows (enforced in SQL, covered by a two-user repo test).
- `GET /api/auth/me` returns `{user_id, role, email, bootstrap_completed}`;
  `POST /api/auth/logout` deletes the current session (→ next request 401).
- `uv run pytest`, `uv run ruff check src tests`, `uv run mypy src` green;
  monorepo `pnpm typecheck` + vitest green.
- Fresh DB boot seeds an admin user + admin session and onboarding runs.
