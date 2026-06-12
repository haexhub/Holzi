# §1 — Postgres Foundation + RLS — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
>
> **Status (2026-06-11):** Tasks 1–17 shipped on branch `feat/section-1-postgres-rls` (PR #87). Tasks 18–22 remain — testcontainers conftest, RLS smoke test, port existing tests, lifespan E2E, README/cleanup. The lifespan boots cleanly against Postgres; the test suite is still SQLite-shaped and largely red until Task 18 wires the new fixtures.

**Goal:** Replace the SQLite/aiosqlite single-box backend with a greenfield Postgres + Alembic data layer enforcing per-user isolation via DB-level Row-Level Security. After this plan, `hermes-server` boots against Postgres, every personal-data table denies cross-user reads/writes at the DB layer, and the platform_admin is seeded from env.

**Architecture:**
- Driver swap `sqlite+aiosqlite` → `postgresql+asyncpg`. SQLAlchemy Core stays.
- Two DB roles: **`holzi_owner`** (owns objects, runs DDL via Alembic) and **`holzi_app`** (`NOSUPERUSER NOBYPASSRLS LOGIN`, what the app connects as). RLS only bites because the app is not the owner.
- A FastAPI `ContextVar[int]` carries the resolved `user_id` per request; every `engine.begin()` is replaced by `tx_for_user(engine)` which issues `SET LOCAL app.user_id = $1` at transaction start. Forgetting `WHERE user_id = …` in app code now returns zero foreign rows instead of leaking.
- FTS5 virtual tables and `_apply_lightweight_migrations` are deleted. Full-text search rides on `tsvector + GIN`. Schema evolves via Alembic.
- `HERMES_AUTH_TOKEN` is renamed to `HERMES_PLATFORM_ADMIN_TOKEN`, gains a companion `HERMES_PLATFORM_ADMIN_EMAIL`, and seeds a `users` row with `role='platform_admin'` plus a never-expiring bootstrap session — same shape as today's bootstrap, new name and role.

**Tech Stack:** Postgres 16+, asyncpg, SQLAlchemy Core async 2.x, Alembic, `testcontainers-python[postgres]`, FastAPI, pytest-asyncio.

**Scope (strict §1, deferred to later sections):**
- §2: orgs, `org_admin` role, `audit_log` table, registration / join policies, magic-link minter, shared-resource scope/status, `app.org_id` GUC.
- §3: LiteLLM, per-user virtual keys, model allowlist.
- §4–§7: memory, learning loop, sandbox hardening, code index.
- §1 leaves `skills` + `mcp_servers` as-is (still global today, §6 will scope them). §1 RLS-locks only the **personal-data** tables explicitly named in the design.

**Personal-data tables that get `user_id` + RLS in §1:** `sessions`, `conversations`, `messages`, `notes`, `agent_tasks`, `personas`, `persona_history`, `attachments`, `agent_runs`, `tool_approvals`, `llm_credentials`. (Direct column on each — denormalized for `messages`/`attachments`/`agent_runs`/`persona_history`, which currently chain through their parents. RLS policies need a column on the row they protect.)

**Non-personal tables (no RLS in §1):** `users`, `channel_prompts` (global config), `workspaces` (slug-keyed infra), `sandbox_crashes` (operator diagnostic), `skills`, `mcp_servers` (handled in §2).

---

## Pre-flight

Before starting, create an isolated worktree (using `superpowers:using-git-worktrees`) on a branch like `feat/section-1-postgres-rls`. Every task below assumes you are inside that worktree. Commit after every task.

---

## Phase A — Infrastructure

### Task 1: Add a Postgres service for dev + tests

**Files:**
- Modify: `docker-compose.local.yml` — add `db` service.
- Modify: `docker-compose.yml` — same, for the production-like compose path.
- Create: `docs/postgres-dev.md` (one short paragraph + `docker compose up db`).

**Step 1: Add the service to both compose files**

Use Postgres 16 image. Listen on `127.0.0.1:5433` to avoid colliding with a host Postgres. Persist data in a named volume:

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: holzi
      POSTGRES_USER: holzi_owner
      POSTGRES_PASSWORD: holzi_owner_dev_pw  # dev only; prod sources from secrets
    ports:
      - "127.0.0.1:5433:5432"
    volumes:
      - holzi-pg:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U holzi_owner -d holzi"]
      interval: 2s
      timeout: 2s
      retries: 20

volumes:
  holzi-pg:
```

Add `depends_on: { db: { condition: service_healthy } }` to the `hermes` service entry in both files.

**Step 2: Bring it up and verify**

Run: `docker compose -f docker-compose.local.yml up -d db && docker compose -f docker-compose.local.yml exec db pg_isready -U holzi_owner -d holzi`
Expected: `accepting connections`.

**Step 3: Commit**

```bash
git add docker-compose.local.yml docker-compose.yml docs/postgres-dev.md
git commit -m "feat(infra): add Postgres dev service"
```

---

### Task 2: Swap aiosqlite → asyncpg in dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Replace deps**

In `[project] dependencies`:
- Remove `"aiosqlite>=0.20"`.
- Add `"asyncpg>=0.30"`.
- Add `"alembic>=1.13"`.
- Add `"psycopg2-binary>=2.9"` — needed for Alembic's offline / sync-mode helpers.

In `[project.optional-dependencies] dev`:
- Add `"testcontainers[postgres]>=4.8"`.

**Step 2: Resolve + commit lockfile**

Run: `uv lock`
Expected: lockfile regenerates with asyncpg / alembic / testcontainers pulled in, aiosqlite gone.

**Step 3: Quick import smoke**

Run: `uv run python -c "import asyncpg, alembic, testcontainers.postgres; print('ok')"`
Expected: `ok`.

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: drop aiosqlite, add asyncpg+alembic+testcontainers"
```

---

### Task 3: New config — `database_url` + platform admin env

**Files:**
- Modify: `src/hermes/config.py`
- Modify: `tests/conftest.py` — set the new env vars where the current tests set `HERMES_AUTH_TOKEN`.

**Step 1: Write the failing test**

Create `tests/test_config_platform_admin.py`:

```python
import importlib
import os

def test_platform_admin_env_vars_required(monkeypatch):
    monkeypatch.delenv("HERMES_PLATFORM_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_AUTH_TOKEN", raising=False)
    import hermes.config as cfg
    importlib.reload(cfg)
    # Without the admin token the Settings() construction must fail loudly.
    try:
        cfg.Settings()  # type: ignore[call-arg]
    except Exception as exc:
        assert "PLATFORM_ADMIN_TOKEN" in str(exc) or "auth" not in str(exc).lower()
        return
    raise AssertionError("Settings should require HERMES_PLATFORM_ADMIN_TOKEN")


def test_database_url_default(monkeypatch):
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "x")
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.delenv("HERMES_DATABASE_URL", raising=False)
    import hermes.config as cfg
    importlib.reload(cfg)
    s = cfg.Settings()  # type: ignore[call-arg]
    assert s.database_url.startswith("postgresql+asyncpg://")
```

**Step 2: Implement**

In `src/hermes/config.py`, replace `auth_token` with the new fields, add `database_url`, remove `db_path`:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HERMES_",
        env_file=".env",
        extra="ignore",
    )

    # The bearer that maps to the env-seeded platform admin (§2 design).
    platform_admin_token: str = Field(..., min_length=1)
    # The email seeded onto the platform_admin user row.
    platform_admin_email: str = Field(..., min_length=3)

    # asyncpg DSN. Dev/test default points at the docker-compose `db` service.
    database_url: str = "postgresql+asyncpg://holzi_owner:holzi_owner_dev_pw@db:5432/holzi"

    # Runtime DSN: the app connects as holzi_app, NOT as the migration owner.
    # When unset, derived from database_url by substituting the role + password.
    runtime_database_url: str | None = None
    runtime_role_password: str = "holzi_app_dev_pw"

    # ... keep everything else (log_*, secret_key, llm_*, sandbox_*, workspace_*) ...
```

Delete `db_path`. Adjust `get_data_dir()` to no longer derive from `db_path` — use a new `HERMES_DATA_DIR` fallback (default `/var/lib/hermes` in prod, cwd in dev/tests).

**Step 3: Update conftest.py**

In `tests/conftest.py`, replace the top-of-file os.environ.setdefault block:

```python
os.environ.setdefault("HERMES_PLATFORM_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("HERMES_PLATFORM_ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("HERMES_LOG_LEVEL", "WARNING")
# DATABASE_URL is provided per-test by the testcontainers fixture (Task 19).
```

Delete the `_TEST_DB_FD` / `_TEST_DB_PATH` lines and the `HERMES_DB_PATH` env. They die with SQLite.

**Step 4: Verify**

Run: `uv run pytest tests/test_config_platform_admin.py -v`
Expected: both tests pass.

**Step 5: Commit**

```bash
git add src/hermes/config.py tests/conftest.py tests/test_config_platform_admin.py
git commit -m "config: platform admin env + database_url, drop db_path"
```

---

## Phase B — Schema port + Alembic

### Task 4: Bootstrap Alembic

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/.gitkeep`.

**Step 1: Initialize**

Run: `uv run alembic init -t async alembic`
Expected: `alembic/` directory + `alembic.ini` created.

**Step 2: Wire env.py to settings**

Edit `alembic/env.py` so it reads the DSN from `hermes.config.settings.database_url` (owner role, since DDL needs ownership) and uses `hermes.schema.metadata` as `target_metadata`:

```python
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

from hermes.config import settings
from hermes.schema import metadata

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    # configure + run share one transaction owned by alembic's
    # begin_transaction(). compare_type + compare_server_default catch
    # column-type swaps (Task 5: Integer→Boolean) that the default diff misses.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as conn:
        await conn.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_migrations_online())
```

**Step 3: Verify**

`hermes.config` validates `HERMES_PLATFORM_ADMIN_TOKEN` + `HERMES_PLATFORM_ADMIN_EMAIL`
at import time, so `alembic` CLI invocations need them in the shell env (or via
`.env`). For local verification against the docker-compose `db` service:

```bash
export HERMES_PLATFORM_ADMIN_TOKEN=dev-token
export HERMES_PLATFORM_ADMIN_EMAIL=dev@local
export HERMES_DATABASE_URL='postgresql+asyncpg://holzi_owner:holzi_owner_dev_pw@127.0.0.1:5433/holzi'
.venv/bin/python -m alembic current
```

Expected: alembic logs `Context impl PostgresqlImpl` + `Will assume transactional DDL`,
no revision listed (none exist yet), exit 0.

**Step 4: Commit**

```bash
git add alembic alembic.ini
git commit -m "alembic: bootstrap async env wired to hermes.schema.metadata"
```

---

### Task 5: Port `schema.py` to Postgres-portable types

**Files:**
- Modify: `src/hermes/schema.py`
- Modify: `src/hermes/repository/notes.py` — replace `sqlite_insert` upsert with `postgresql.insert(...).on_conflict_do_update(...)`.

**Step 1: Write the failing tests (use existing schema metadata)**

Create `tests/test_schema_postgres.py`:

```python
from sqlalchemy import Boolean
from hermes.schema import (
    conversations, messages, notes, agent_tasks, personas, llm_credentials,
    sessions, users, attachments, agent_runs, persona_history, tool_approvals,
)

def test_boolean_columns_are_real_bool():
    for col in (conversations.c.bookmarked, agent_tasks.c.enabled,
                personas.c.is_default, llm_credentials.c.is_active,
                users.c.bootstrap_completed):
        assert isinstance(col.type, Boolean), f"{col} should be Boolean"

def test_personal_tables_carry_user_id():
    for table in (conversations, messages, notes, agent_tasks, personas,
                  attachments, agent_runs, persona_history, tool_approvals,
                  llm_credentials, sessions):
        assert "user_id" in table.c, f"{table.name} missing user_id"
```

**Step 2: Edit `schema.py`**

For every personal table, do:
- Replace `Column("flag", Integer, ..., server_default="0")` with `Column("flag", Boolean, nullable=False, server_default=text("false"))`. Affected: `conversations.bookmarked`, `agent_tasks.enabled`, `personas.is_default`, `llm_credentials.is_active`, `skills.enabled`, `mcp_servers.enabled`, `users.bootstrap_completed`.
- Drop SQLite `server_default="1"` integer-as-user-id backfill defaults — fresh DB, no legacy backfill needed. Replace with `nullable=False` and let the repo layer pass `user_id` explicitly.
- For columns currently typed `Integer` that hold unix-epoch seconds, leave them as `Integer` for now (portable; revisit `TIMESTAMPTZ` in §2). Add a comment noting the tradeoff.
- `users.role`: change `server_default="member"` so the valid runtime values become `{'platform_admin', 'member'}`. Add a CHECK constraint:
  ```python
  CheckConstraint("role IN ('platform_admin','member')", name="users_role_valid")
  ```
  (`org_admin` is added in §2; do not pre-emptively include it.)

For tables missing `user_id` today, add the column **NOT NULL, FK to users(id) ON DELETE CASCADE**:
- `messages.user_id` (denormalized from `conversations.user_id`)
- `attachments.user_id` (denormalized from `conversations.user_id`)
- `agent_runs.user_id` (denormalized from `conversations.user_id`)
- `persona_history.user_id` (denormalized from `personas.user_id`)
- `tool_approvals.user_id` — and change the PK to **composite `(user_id, tool_name)`** so the same tool can be approved per-user.
- `llm_credentials.user_id` — and drop the existing global partial unique index `llm_credentials_active_uq`; replace with the equivalent scoped to `(user_id) WHERE is_active = true` (declared in Alembic, not here — see Task 7).

For each new column, add the per-user composite index that already exists on the user-id-carrying tables (see existing `conv_user_updated`, `notes_user`, etc.) — declare them on the Table now that the column is born NOT NULL.

Delete the long block of NOTE comments referencing pre-C1 migration order — irrelevant on greenfield.

**Step 3: Update `repository/notes.py::upsert`**

Replace `from sqlalchemy.dialects.sqlite import insert as sqlite_insert` with `from sqlalchemy.dialects.postgresql import insert as pg_insert`. The `.on_conflict_do_update(index_elements=…)` API is identical.

**Step 4: Verify**

Run: `uv run pytest tests/test_schema_postgres.py -v`
Expected: both tests pass.

**Step 5: Commit**

```bash
git add src/hermes/schema.py src/hermes/repository/notes.py tests/test_schema_postgres.py
git commit -m "schema: port to Postgres-portable types, denormalize user_id"
```

---

### Task 6: First Alembic revision — `0001_initial.py`

**Files:**
- Create: `alembic/versions/0001_initial.py`

**Step 1: Autogenerate**

Run: `uv run alembic revision --autogenerate -m "initial schema"`
Expected: a file `alembic/versions/0001_*.py` containing `op.create_table(...)` for every table in `metadata`.

**Step 2: Hand-review and fix**

Open the generated file and:
- Rename it to `0001_initial.py` (drop the random hash prefix for stable ordering).
- Verify every personal-data table has `user_id` NOT NULL with a FK to `users(id)` ON DELETE CASCADE.
- Verify Booleans, not Integers, for the flag columns.
- Verify the per-user composite indexes (`conv_user_updated`, `notes_user`, `agent_tasks_user_enabled_due`, `personas_user_default`, `sessions_user`) come through.
- For `llm_credentials`, replace any autogenerated unique on `is_active` with a **per-user partial unique**:
  ```python
  op.create_index(
      "llm_credentials_user_active_uq",
      "llm_credentials",
      ["user_id"],
      unique=True,
      postgresql_where=sa.text("is_active = true"),
  )
  ```

**Step 3: Verify migration applies cleanly**

```bash
# nuke the dev DB volume to start clean
docker compose -f docker-compose.local.yml down -v db
docker compose -f docker-compose.local.yml up -d db
uv run alembic upgrade head
uv run alembic current
```
Expected: `0001_initial (head)`.

**Step 4: Verify downgrade**

Run: `uv run alembic downgrade base && uv run alembic upgrade head`
Expected: clean down + up.

**Step 5: Commit**

```bash
git add alembic/versions/0001_initial.py
git commit -m "alembic: initial schema revision"
```

---

### Task 7: Second Alembic revision — `0002_roles.py` (create `holzi_app`)

**Files:**
- Create: `alembic/versions/0002_roles.py`

**Step 1: Write the revision**

This revision creates the runtime role and grants it `INSERT/SELECT/UPDATE/DELETE` on every table — but **not** ownership.

```python
"""create holzi_app runtime role
Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"

# Tables holzi_app needs DML on. Excludes nothing — RLS, not GRANT, is the
# isolation mechanism. The role is the *vehicle* RLS uses (NOBYPASSRLS).
RUNTIME_TABLES = [
    "users", "sessions",
    "conversations", "messages", "attachments", "agent_runs",
    "notes", "agent_tasks", "personas", "persona_history",
    "channel_prompts", "llm_credentials", "skills", "mcp_servers",
    "tool_approvals", "workspaces", "sandbox_crashes",
]


def upgrade() -> None:
    # Idempotent role creation. Password comes from a settings-supplied
    # SQL parameter so dev/prod can differ. Alembic doesn't bind via psycopg
    # for DO blocks — embed the literal but keep the dev value low-trust.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'holzi_app') THEN
                CREATE ROLE holzi_app
                    LOGIN
                    NOSUPERUSER
                    NOBYPASSRLS
                    PASSWORD 'holzi_app_dev_pw';
            END IF;
        END$$;
    """)
    op.execute("GRANT CONNECT ON DATABASE holzi TO holzi_app;")
    op.execute("GRANT USAGE ON SCHEMA public TO holzi_app;")
    for t in RUNTIME_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO holzi_app;")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO holzi_app;")
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO holzi_app;
    """)
    op.execute("""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO holzi_app;
    """)


def downgrade() -> None:
    for t in reversed(RUNTIME_TABLES):
        op.execute(f"REVOKE ALL ON {t} FROM holzi_app;")
    op.execute("REVOKE USAGE ON SCHEMA public FROM holzi_app;")
    op.execute("REVOKE CONNECT ON DATABASE holzi FROM holzi_app;")
    op.execute("DROP ROLE IF EXISTS holzi_app;")
```

**Step 2: Verify**

Run: `uv run alembic upgrade head` then in psql as the postgres super-user:

```bash
docker compose -f docker-compose.local.yml exec db psql -U holzi_owner -d holzi \
    -c "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles WHERE rolname='holzi_app';"
```
Expected: `holzi_app | f | f` (no bypass, no super).

**Step 3: Commit**

```bash
git add alembic/versions/0002_roles.py
git commit -m "alembic: create holzi_app runtime role (NOBYPASSRLS)"
```

---

### Task 8: Third Alembic revision — `0003_rls.py` (enable RLS + policies)

**Files:**
- Create: `alembic/versions/0003_rls.py`

**Step 1: Write the revision**

```python
"""enable RLS + per-user USING policies on personal tables
Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"

PERSONAL_TABLES = [
    "sessions",
    "conversations", "messages", "attachments", "agent_runs",
    "notes", "agent_tasks", "personas", "persona_history",
    "tool_approvals", "llm_credentials",
]


def upgrade() -> None:
    # The GUC must exist for `current_setting(..., true)` to return NULL
    # when unset (instead of raising). Define it at the database level so
    # every connection sees it; `SET LOCAL` will override per-transaction.
    op.execute("ALTER DATABASE holzi SET app.user_id TO '0';")

    for t in PERSONAL_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
        # FORCE makes the policy apply even to the table's owner (holzi_owner).
        # Without FORCE, the owner sees everything — the policy would only
        # protect holzi_app, and any future code path that opens an owner
        # connection (Alembic, ad-hoc psql) would bypass isolation.
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY {t}_user_isolation ON {t}
                USING (user_id = current_setting('app.user_id', true)::bigint)
                WITH CHECK (user_id = current_setting('app.user_id', true)::bigint);
        """)


def downgrade() -> None:
    for t in reversed(PERSONAL_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {t}_user_isolation ON {t};")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER DATABASE holzi RESET app.user_id;")
```

Why `WITH CHECK` matches `USING`: forbids INSERT/UPDATE that writes a row a future SELECT couldn't see. Without it, user A could `INSERT INTO notes (user_id, …) VALUES (B, …)` and the policy wouldn't catch it on write.

**Step 2: Verify policy is in place**

```bash
uv run alembic upgrade head
docker compose -f docker-compose.local.yml exec db psql -U holzi_owner -d holzi \
    -c "SELECT tablename, policyname FROM pg_policies WHERE schemaname='public' ORDER BY tablename;"
```
Expected: 11 rows, one policy per personal table.

**Step 3: Commit**

```bash
git add alembic/versions/0003_rls.py
git commit -m "alembic: enable RLS + per-user policies on personal tables"
```

---

### Task 9: Fourth Alembic revision — `0004_tsvector_fts.py` (kill FTS5, add tsvector + GIN)

**Files:**
- Create: `alembic/versions/0004_tsvector_fts.py`

**Step 1: Write the revision**

Use generated columns so search vectors stay in sync without app help.

```python
"""tsvector + GIN replaces FTS5
Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    # messages.content
    op.execute("""
        ALTER TABLE messages
            ADD COLUMN content_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED;
    """)
    op.execute("CREATE INDEX messages_content_tsv ON messages USING GIN (content_tsv);")

    # notes.key + content + tags (the old notes_fts indexed all three).
    op.execute("""
        ALTER TABLE notes
            ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(key, '') || ' ' ||
                    coalesce(content, '') || ' ' ||
                    coalesce(tags, ''))
            ) STORED;
    """)
    op.execute("CREATE INDEX notes_search_tsv ON notes USING GIN (search_tsv);")

    # skills (slug + name + description + when_to_use + body_markdown).
    op.execute("""
        ALTER TABLE skills
            ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(slug, '') || ' ' ||
                    coalesce(name, '') || ' ' ||
                    coalesce(description, '') || ' ' ||
                    coalesce(when_to_use, '') || ' ' ||
                    coalesce(body_markdown, ''))
            ) STORED;
    """)
    op.execute("CREATE INDEX skills_search_tsv ON skills USING GIN (search_tsv);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS skills_search_tsv;")
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS search_tsv;")
    op.execute("DROP INDEX IF EXISTS notes_search_tsv;")
    op.execute("ALTER TABLE notes DROP COLUMN IF EXISTS search_tsv;")
    op.execute("DROP INDEX IF EXISTS messages_content_tsv;")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS content_tsv;")
```

**Step 2: Verify**

Run: `uv run alembic upgrade head` then:

```bash
docker compose -f docker-compose.local.yml exec db psql -U holzi_owner -d holzi \
    -c "\d+ messages" | grep tsv
```
Expected: `content_tsv | tsvector | … generated always as … stored`.

**Step 3: Commit**

```bash
git add alembic/versions/0004_tsvector_fts.py
git commit -m "alembic: tsvector + GIN replace FTS5"
```

---

## Phase C — App connection + RLS context

### Task 10: Rewrite `db.py` for asyncpg + Alembic-driven schema

**Files:**
- Modify: `src/hermes/db.py` (delete most of it)

**Step 1: Strip the file down**

The new responsibilities of `db.py`:
1. Open one `AsyncEngine` against `settings.runtime_database_url` (the `holzi_app` DSN — derived from `database_url` if `runtime_database_url` is None).
2. Run `alembic upgrade head` once at boot (use `alembic.command.upgrade(config, "head")` against the **owner** DSN — `settings.database_url`).
3. Provide the `tx_for_user()` async context manager.

Replace the entire `src/hermes/db.py` with roughly:

```python
"""Database bootstrap (Postgres + RLS).

`init_db()` runs Alembic to `head` (using the owner role) and returns an
AsyncEngine that connects as `holzi_app` — the role with NOBYPASSRLS, the
one RLS actually bites. Per-request code uses `tx_for_user(engine)` to open
a transaction with `SET LOCAL app.user_id = $1` applied; the resolved
user_id is read from the `current_user_id` ContextVar populated by the
auth middleware.
"""
import contextlib
from contextvars import ContextVar
from typing import AsyncIterator

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from hermes.config import settings


_current_user_id: ContextVar[int | None] = ContextVar("_current_user_id", default=None)


def set_current_user(user_id: int | None) -> None:
    _current_user_id.set(user_id)


def get_current_user() -> int | None:
    return _current_user_id.get()


def _owner_url() -> str:
    return settings.database_url


def _runtime_url() -> str:
    if settings.runtime_database_url:
        return settings.runtime_database_url
    # Derive: same host/db, swap role + password.
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(settings.database_url)
    netloc = f"holzi_app:{settings.runtime_role_password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def init_db() -> AsyncEngine:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _owner_url())
    # Alembic 1.13 has no native async runner from inside an event loop:
    # run upgrade in a thread to keep the event loop clean.
    import asyncio
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_async_engine(_runtime_url(), pool_pre_ping=True)
    return engine


@contextlib.asynccontextmanager
async def tx_for_user(engine: AsyncEngine, *, user_id: int | None = None) -> AsyncIterator[AsyncConnection]:
    """Open a transaction with `SET LOCAL app.user_id = $1`.

    Resolution order: explicit `user_id` arg > ContextVar > raise.
    The middleware populates the ContextVar; repository code that runs
    outside a request (lifespan seeding, the scheduler) must pass `user_id`
    explicitly. A None resolution is a programming error — RLS would silently
    return zero rows, which is the worst possible failure mode.
    """
    uid = user_id if user_id is not None else _current_user_id.get()
    if uid is None:
        raise RuntimeError(
            "tx_for_user requires a resolved user_id "
            "(ContextVar empty and no explicit kwarg)"
        )
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.user_id = :u"), {"u": str(uid)})
        yield conn


@contextlib.asynccontextmanager
async def tx_as_owner(owner_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Escape-hatch for lifespan bootstrap (seed platform admin) and the
    scheduler's GLOBAL queries (e.g. `list_expired`, `agent_tasks list_due`).
    Connects as `holzi_owner` — bypasses RLS by definition (FORCE applies to
    OWNER too, but bootstrap explicitly SETs app.user_id when needed).
    Callers are responsible for safety.
    """
    async with owner_engine.begin() as conn:
        yield conn


async def make_owner_engine() -> AsyncEngine:
    """Separate engine for the rare owner-role paths above. Disposed by lifespan."""
    return create_async_engine(_owner_url(), pool_pre_ping=True, pool_size=2)
```

Delete `_apply_lightweight_migrations`, `_FTS_SCHEMA_SQL`, `_split_statements`, the SQLite PRAGMA event listener — all dead.

**Step 2: Write a quick integration test**

Create `tests/test_db_postgres.py`:

```python
import pytest
from sqlalchemy import text

@pytest.mark.usefixtures("pg_db")  # provided by Task 19
async def test_init_db_runs_migrations_and_returns_engine():
    from hermes.db import init_db
    engine = await init_db()
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT current_user, current_setting('app.user_id', true)"
            ))).first()
            assert row[0] == "holzi_app"
    finally:
        await engine.dispose()
```

This needs the `pg_db` fixture from Task 19 to run; flag it pending and revisit after Task 19.

**Step 3: Commit**

```bash
git add src/hermes/db.py tests/test_db_postgres.py
git commit -m "db: asyncpg engine + tx_for_user RLS context"
```

---

### Task 11: Refactor repository functions to use `tx_for_user()`

**Files:**
- Modify: every file in `src/hermes/repository/` that calls `engine.begin()` or `engine.connect()` with a `user_id` filter — i.e. `conversations.py`, `messages.py`, `notes.py`, `agent_tasks.py`, `personas.py`, `persona_history.py`, `attachments.py`, `runs.py` (agent_runs), `approvals.py`, `llm_credentials.py`.

**Step 1: Pattern**

Every place that looks like:

```python
async with engine.begin() as conn:
    await conn.execute(t_notes.insert().values(user_id=user_id, …))
```

becomes:

```python
from hermes.db import tx_for_user

async with tx_for_user(engine, user_id=user_id) as conn:
    await conn.execute(t_notes.insert().values(user_id=user_id, …))
```

Read-side `engine.connect()` paths convert to the same helper (a transaction is needed for `SET LOCAL`):

```python
async with tx_for_user(engine, user_id=user_id) as conn:
    result = await conn.execute(select(t_notes).where(t_notes.c.user_id == user_id))
```

The `WHERE user_id = …` filters stay — defense-in-depth alongside RLS. The plan is *not* to remove the app-layer filters; RLS is the floor, the filters keep query plans tight.

**Step 2: Special cases**

- `repository/conversations.py::list_expired`, `sweep_expired` — GLOBAL queries (one scheduler serves every user). Switch to `tx_as_owner(owner_engine)` with an explicit comment that RLS is intentionally bypassed for sweep semantics.
- `repository/agent_tasks.py::list_due` — same global-sweep pattern.
- `repository/messages.py::fts_search` — when `conversation_id is None`, the call is implicitly cross-conversation; if the caller has a `user_id`, thread it through. Add a `user_id: int` kwarg.

Add `user_id` as a required kwarg to `repository/messages.py::append`, `list_by_conversation`, `update_content`, `delete_after`, `fts_search` — denormalized column means the writer must supply it (look up `conversations.user_id` once in the route layer).

**Step 3: Verify per file**

For each repo file, after editing, run the matching test file:
```bash
uv run pytest tests/test_conversations.py -v
uv run pytest tests/test_notes.py -v
…
```
(After Task 19 wires the Postgres test fixture. Until then this task lands but its tests run red — that's expected.)

**Step 4: Commit (one commit per repo file)**

```bash
git add src/hermes/repository/notes.py
git commit -m "repo(notes): use tx_for_user for RLS context"
```
Repeat for each file. Frequent small commits keep the diff reviewable.

---

### Task 12: Auth middleware populates the ContextVar

**Files:**
- Modify: `src/hermes/auth.py`
- Modify: `src/hermes/main.py` — websocket entrypoint (`/ws/agent`) and lifespan-side scheduler bootstrap.

**Step 1: Write the failing test**

Create `tests/test_auth_contextvar.py`:

```python
import pytest
from hermes.db import get_current_user

@pytest.mark.usefixtures("app_with_pg")  # provided by Task 19
async def test_authenticated_request_populates_contextvar(client):
    # The test client's app exposes a probe route that returns get_current_user().
    r = await client.get("/__test/whoami", headers={"Authorization": "Bearer test-admin-token"})
    assert r.status_code == 200
    assert r.json() == {"user_id": 1}
```

You'll add the `/__test/whoami` route conditionally in tests-only via a fixture (or pin it behind `if os.environ.get("HERMES_TEST_PROBES")`).

**Step 2: Implement middleware change**

In `src/hermes/auth.py`, after resolving `identity`:

```python
from hermes.db import set_current_user

…
identity = await request.app.state.identity_resolver.resolve(provided)
if identity is None:
    return _unauthorized(request, reason="invalid_or_expired_session")

token = set_current_user_token(identity.user_id)  # see Step 3
request.state.user_id = identity.user_id
request.state.role = identity.role
try:
    return await call_next(request)
finally:
    reset_current_user(token)
```

ContextVars and `set` return a `Token` you must reset to restore the previous value — important because Starlette reuses the same async task for the response phase, and a leaked value would bleed into the next request. Add helper functions in `db.py`:

```python
def set_current_user_token(user_id: int):
    return _current_user_id.set(user_id)

def reset_current_user(token) -> None:
    _current_user_id.reset(token)
```

**Step 3: WebSocket handler**

In `src/hermes/main.py` (or wherever `/ws/agent` is mounted — `grep -rn "ws/agent"`), resolve the bearer the same way at handshake and apply `set_current_user_token` for the duration of the connection (`try/finally`).

**Step 4: Verify**

After Task 19 lands the test fixture, run:
```bash
uv run pytest tests/test_auth_contextvar.py -v
```
Expected: pass.

**Step 5: Commit**

```bash
git add src/hermes/auth.py src/hermes/main.py src/hermes/db.py tests/test_auth_contextvar.py
git commit -m "auth: populate current_user ContextVar for tx_for_user"
```

---

## Phase D — FTS migration (apply tsvector in repository search paths)

### Task 13: `repository/messages.py::fts_search` — tsvector

**Files:**
- Modify: `src/hermes/repository/messages.py`
- Modify: `tests/test_messages.py`

**Step 1: Write the failing test**

In `tests/test_messages.py`, add or replace a search test:

```python
async def test_fts_search_uses_tsvector(engine, seed_user):
    # Helper inserts a message via the conversation pipeline.
    await _seed_message(engine, content="visit the dentist on tuesday", user_id=seed_user)
    from hermes.repository.messages import fts_search
    results = await fts_search(engine, query="dent:*", user_id=seed_user)
    assert any("dentist" in m.content for m in results)
```

**Step 2: Rewrite `fts_search`**

```python
async def fts_search(
    engine: AsyncEngine,
    *,
    user_id: int,
    query: str,
    conversation_id: int | None = None,
    limit: int = 10,
) -> list[Message]:
    sql_base = (
        "SELECT id, conversation_id, role, content, ts, meta_json "
        "FROM messages "
        "WHERE content_tsv @@ to_tsquery('simple', :q) "
        "AND user_id = :uid"
    )
    params = {"q": query, "uid": user_id, "limit": limit}
    if conversation_id is not None:
        sql_base += " AND conversation_id = :cid"
        params["cid"] = conversation_id
    sql_base += " ORDER BY ts DESC LIMIT :limit"
    async with tx_for_user(engine, user_id=user_id) as conn:
        result = await conn.execute(text(sql_base), params)
        rows = result.all()
    return [_row_to_message(r) for r in rows]
```

The `user_id = :uid` filter is defense-in-depth — RLS already filters via the policy. Keep it.

**Step 3: Update the caller-side tokenizer**

Callers used to pass FTS5 expressions like `"dent*"`. The Postgres equivalent is `dent:*`. Find every call site of `fts_search` (`grep -rn "fts_search" src/`) — there's probably one in the tool layer. Update the token construction to emit `:*` suffix and to escape special chars (`& | ! ( ) <-> :`) with backslashes.

**Step 4: Verify**

Run: `uv run pytest tests/test_messages.py -v`
Expected: pass.

**Step 5: Commit**

```bash
git add src/hermes/repository/messages.py tests/test_messages.py
git commit -m "repo(messages): tsvector full-text search"
```

---

### Task 14: `repository/notes.py::find` — tsvector

**Files:**
- Modify: `src/hermes/repository/notes.py`
- Modify: `tests/test_notes.py`

Mirror Task 13 against `notes.search_tsv`.

```python
sql = text(
    "SELECT id, key, content, tags, updated_at, user_id "
    "FROM notes "
    "WHERE search_tsv @@ to_tsquery('simple', :q) AND user_id = :uid "
    "ORDER BY ts_rank(search_tsv, to_tsquery('simple', :q)) DESC "
    "LIMIT :limit"
)
```

Same test pattern (assert a query for `dent:*` returns the seeded `dentist` note). Same commit shape.

---

### Task 15: `repository/conversations.py::search` — tsvector

**Files:**
- Modify: `src/hermes/repository/conversations.py`
- Modify: `tests/test_conversations.py`

The current implementation uses FTS5 prefix `tok*` joined back to `conversations` via `messages_fts.rowid = m.id`. The Postgres equivalent uses `messages.content_tsv @@ to_tsquery('simple', :q)` joined back to `conversations` on `m.conversation_id`. The title-LIKE fallback stays.

Update `_FTS_TOKEN_RE` consumers — same regex extracts the tokens, but the join string changes:

```python
conditions = [
    "(" + " OR ".join(title_clauses) + ")",
    "c.id IN (SELECT m.conversation_id FROM messages m "
    "WHERE m.content_tsv @@ to_tsquery('simple', :fts_q))",
]
fts_match = " | ".join(f"{t}:*" for t in tokens)
```

Sanitize tokens: strip everything but `\w` (the regex already does this).

Commit:
```bash
git add src/hermes/repository/conversations.py tests/test_conversations.py
git commit -m "repo(conversations): tsvector full-text search"
```

---

### Task 16: Delete `schema.sql` + dead FTS plumbing

**Files:**
- Delete: `src/hermes/schema.sql`
- Modify: `src/hermes/__init__.py` and `pyproject.toml` — drop `schema.sql` from the wheel manifest.

**Step 1: Check that nothing imports it**

Run: `grep -rn "schema.sql\|_FTS_SCHEMA_SQL\|messages_fts\|notes_fts\|skills_fts\|proxy_credentials_v1" src/ tests/`
Expected: only references inside files this plan is about to delete or modify.

**Step 2: Address `proxy_credentials_v1`**

The view is used by the `haex-claude-proxy` sqlite-resolver — but that's the SQLite-resolver from the C1 era. Wave C / the SaaS pivot uses LiteLLM-fronted credentials (§3), not the view. For §1 we drop the view from the schema and **delete `haex-claude-proxy-resolver-sqlite` from the credential resolution path entirely**.

In `src/hermes/upstream.py` (and anywhere `rebuild_upstream_from_db` lives), confirm the proxy view is not read directly. If it is, switch to plain `SELECT` against `llm_credentials` filtered by `is_active = true AND user_id = :uid`.

**Step 3: Delete**

```bash
git rm src/hermes/schema.sql
```

Edit `[tool.hatch.build]` package data in `pyproject.toml` if it explicitly lists `schema.sql`.

**Step 4: Commit**

```bash
git add -A
git commit -m "cleanup: delete SQLite-only schema.sql + FTS plumbing"
```

---

## Phase E — Bootstrap rewrite

### Task 17: Rewrite `users.py::ensure_users_seeded` → `ensure_platform_admin_seeded`

**Files:**
- Modify: `src/hermes/users.py`
- Modify: `src/hermes/main.py` — update lifespan call site.

**Step 1: Write the failing test**

Create `tests/test_platform_admin_seed.py`:

```python
import pytest
from sqlalchemy import text
from hermes.identity import hash_token

@pytest.mark.usefixtures("pg_db")
async def test_platform_admin_seeded_from_env(owner_engine, monkeypatch):
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "rotated-token")
    # Reload settings so the env is picked up.
    import importlib, hermes.config
    importlib.reload(hermes.config)
    from hermes.users import ensure_platform_admin_seeded
    await ensure_platform_admin_seeded(owner_engine)
    async with owner_engine.connect() as conn:
        u = (await conn.execute(text(
            "SELECT id, email, role FROM users WHERE email='admin@example.com'"
        ))).first()
        assert u.role == "platform_admin"
        s = (await conn.execute(text(
            "SELECT user_id FROM sessions WHERE token_hash=:h"
        ), {"h": hash_token("rotated-token")})).first()
        assert s.user_id == u.id

@pytest.mark.usefixtures("pg_db")
async def test_admin_token_rotation_drops_stale_session(owner_engine, monkeypatch):
    # Seed once with token A, again with token B; expect only B's hash to remain.
    ...
```

**Step 2: Implement**

```python
"""Platform admin bootstrap (§1, refined by §2).

Single source of truth for the env-seeded `platform_admin`: a users row
(email + role='platform_admin'), idempotent, plus a never-expiring session
mapping HERMES_PLATFORM_ADMIN_TOKEN → that user. Rotating the env token
drops the previous bootstrap session so the old token stops working.
"""
import time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes.config import settings
from hermes.identity import hash_token

BOOTSTRAP_LABEL = "bootstrap platform_admin"


async def ensure_platform_admin_seeded(owner_engine: AsyncEngine) -> int:
    """Idempotent. Returns the platform_admin's user id. Run from lifespan
    against the OWNER engine — bypasses RLS by design, since at boot there
    is no resolved user yet.
    """
    now = int(time.time())
    token_hash = hash_token(settings.platform_admin_token)
    email = settings.platform_admin_email

    async with owner_engine.begin() as conn:
        row = (await conn.execute(text(
            "INSERT INTO users(email, role, bootstrap_completed, created_at) "
            "VALUES (:e, 'platform_admin', false, :now) "
            "ON CONFLICT (email) DO UPDATE SET role = 'platform_admin' "
            "RETURNING id"
        ), {"e": email, "now": now})).first()
        user_id = row.id

        await conn.execute(text(
            "DELETE FROM sessions WHERE label = :l AND token_hash != :h"
        ), {"l": BOOTSTRAP_LABEL, "h": token_hash})

        await conn.execute(text(
            "INSERT INTO sessions(user_id, token_hash, label, created_at, expires_at) "
            "VALUES (:uid, :h, :l, :now, NULL) "
            "ON CONFLICT (token_hash) DO NOTHING"
        ), {"uid": user_id, "h": token_hash, "l": BOOTSTRAP_LABEL, "now": now})

    return user_id


async def is_bootstrap_completed(owner_engine: AsyncEngine, user_id: int) -> bool:
    async with owner_engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT bootstrap_completed FROM users WHERE id = :uid"
        ), {"uid": user_id})).first()
    return bool(row and row.bootstrap_completed)
```

**Step 3: Update lifespan call site**

In `src/hermes/main.py`:
- Replace `await ensure_users_seeded(app.state.db)` with `await ensure_platform_admin_seeded(app.state.owner_db)`.
- Add `app.state.owner_db = await make_owner_engine()` after `init_db()`.
- Dispose the owner engine in the lifespan teardown branch.

**Step 4: Verify**

Run: `uv run pytest tests/test_platform_admin_seed.py -v`
Expected: pass.

**Step 5: Commit**

```bash
git add src/hermes/users.py src/hermes/main.py tests/test_platform_admin_seed.py
git commit -m "bootstrap: seed platform_admin from HERMES_PLATFORM_ADMIN_{EMAIL,TOKEN}"
```

---

## Phase F — Test infrastructure + verification

### Task 18: `conftest.py` — testcontainers Postgres + per-test DB isolation

**Files:**
- Modify: `tests/conftest.py`

This is the linchpin that unblocks every other test added by this plan. Implement *after* Tasks 1–17 are mechanically in place (you can stub `pg_db` to skip until now).

**Step 1: Replace the SQLite fixtures**

```python
import asyncio
import os
import secrets
import tempfile

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

# Module-scoped Postgres container — one per pytest session.
@pytest.fixture(scope="session")
def _pg_container():
    # 16-alpine matches docker-compose.
    with PostgresContainer(
        "postgres:16-alpine",
        username="holzi_owner",
        password="holzi_owner_test_pw",
        dbname="holzi_template",
    ) as pg:
        yield pg


def _admin_dsn(pg, dbname: str) -> str:
    host = pg.get_container_host_ip()
    port = pg.get_exposed_port(5432)
    return f"postgresql+asyncpg://holzi_owner:holzi_owner_test_pw@{host}:{port}/{dbname}"


def _app_dsn(pg, dbname: str) -> str:
    host = pg.get_container_host_ip()
    port = pg.get_exposed_port(5432)
    return f"postgresql+asyncpg://holzi_app:holzi_app_test_pw@{host}:{port}/{dbname}"


@pytest.fixture
async def pg_db(_pg_container, monkeypatch):
    """Per-test fresh database. Creates a DB, runs Alembic upgrade head,
    yields the DSN-pair, drops the DB on teardown.
    """
    dbname = "t_" + secrets.token_hex(8)
    admin_template_url = _pg_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql"
    )
    # Create the per-test DB via a sync psycopg connection (asyncpg can't run
    # CREATE DATABASE inside a transaction).
    import psycopg2
    conn = psycopg2.connect(admin_template_url + "?sslmode=disable")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {dbname} OWNER holzi_owner;")
    conn.close()

    owner_url = _admin_dsn(_pg_container, dbname)
    app_url = _app_dsn(_pg_container, dbname)

    monkeypatch.setenv("HERMES_DATABASE_URL", owner_url)
    monkeypatch.setenv("HERMES_RUNTIME_DATABASE_URL", app_url)

    # Reload settings so the env takes effect.
    import importlib, hermes.config
    importlib.reload(hermes.config)

    # Run Alembic to head — this also creates the holzi_app role (revision 0002)
    # the first time the container is used.
    from alembic.config import Config
    from alembic import command
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", owner_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    yield {"owner_url": owner_url, "app_url": app_url, "dbname": dbname}

    # Drop the per-test DB. Disconnect any lingering pool first.
    conn = psycopg2.connect(admin_template_url + "?sslmode=disable")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{dbname}' AND pid <> pg_backend_pid();"
        )
        cur.execute(f"DROP DATABASE {dbname};")
    conn.close()


@pytest.fixture
async def engine(pg_db):
    """AsyncEngine connected as holzi_app (subject to RLS)."""
    e = create_async_engine(pg_db["app_url"], pool_pre_ping=True)
    try:
        yield e
    finally:
        await e.dispose()


@pytest.fixture
async def owner_engine(pg_db):
    """AsyncEngine connected as holzi_owner. Use for seeding fixtures
    that must bypass RLS (creating users for the smoke tests, etc.).
    """
    e = create_async_engine(pg_db["owner_url"], pool_pre_ping=True)
    try:
        yield e
    finally:
        await e.dispose()


@pytest.fixture
async def seed_user(owner_engine):
    """Insert a regular member user, return its id."""
    async with owner_engine.begin() as conn:
        row = (await conn.execute(text(
            "INSERT INTO users(email, role, bootstrap_completed, created_at) "
            "VALUES (:e, 'member', false, 0) RETURNING id"
        ), {"e": f"u_{secrets.token_hex(4)}@test.local"})).first()
    return row.id
```

Delete the existing `conn` fixture, the `_reset_app_db_path` fixture, and the SQLite tmp_path scaffolding.

**Step 2: Update the persona/credential autouse fixture**

The autouse `_patch_persona_context_for_app_tests` fixture currently builds a SENTINEL credential and monkeypatches `routes.api`. Keep its shape, but it will be triggered against the new `pg_db` indirectly via `app_with_pg`.

**Step 3: Verify the container boots once**

Run: `uv run pytest tests/test_db_postgres.py -v`
Expected: pass (testcontainers pulls postgres:16-alpine on first run — slow first time).

**Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "tests: switch fixtures to testcontainers Postgres"
```

---

### Task 19: Cross-user RLS denial smoke test

**Files:**
- Create: `tests/test_rls_cross_user.py`

This is the gate the design doc explicitly calls out: *"RLS smoke test (cross-user denial) before opening to a second user."*

**Step 1: Write the test**

```python
"""Cross-user denial smoke test (§1 design doc requirement).

For every personal-data table, prove that a second user, connecting via
the holzi_app role, cannot see / update / delete user 1's rows even with
an exploit-style query that omits the WHERE clause. RLS is the only thing
standing between them.
"""
import pytest
from sqlalchemy import text
from hermes.db import tx_for_user


PERSONAL_TABLES_WITH_INSERT = [
    # (table, columns, value-factory for an extra-of-this-user row)
    ("notes",         "user_id, key, content, updated_at", "{uid}, 'k_{uid}', 'c_{uid}', 0"),
    ("conversations", "user_id, channel, started_at, updated_at", "{uid}, 'web', 0, 0"),
    ("personas",      "user_id, name, soul, identity, agents, created_at, updated_at",
                      "{uid}, 'p_{uid}', '', '', '', 0, 0"),
    ("agent_tasks",   "user_id, title, prompt, timezone, enabled, created_at, updated_at",
                      "{uid}, 't_{uid}', 'p', 'UTC', true, 0, 0"),
]


@pytest.mark.parametrize("table,cols,values", PERSONAL_TABLES_WITH_INSERT)
async def test_user_b_cannot_read_user_a_rows(engine, owner_engine, table, cols, values):
    # Seed two real users via the owner engine (bypasses RLS — fine for setup).
    async with owner_engine.begin() as conn:
        a = (await conn.execute(text(
            "INSERT INTO users(email, role, bootstrap_completed, created_at) "
            "VALUES ('a@t', 'member', false, 0) RETURNING id"
        ))).first().id
        b = (await conn.execute(text(
            "INSERT INTO users(email, role, bootstrap_completed, created_at) "
            "VALUES ('b@t', 'member', false, 0) RETURNING id"
        ))).first().id

    # User A inserts via the RLS-bound app engine.
    a_values = values.format(uid=a)
    async with tx_for_user(engine, user_id=a) as conn:
        await conn.execute(text(f"INSERT INTO {table}({cols}) VALUES ({a_values})"))

    # User B reads via the same app engine — should see nothing of A's.
    async with tx_for_user(engine, user_id=b) as conn:
        rows = (await conn.execute(text(f"SELECT * FROM {table}"))).all()
        assert rows == [], (
            f"RLS leak: user {b} saw user {a}'s row in {table}: {rows}"
        )


@pytest.mark.parametrize("table,cols,values", PERSONAL_TABLES_WITH_INSERT)
async def test_user_b_cannot_update_user_a_rows(engine, owner_engine, table, cols, values):
    # ... same seeding pattern ...
    # Then:
    async with tx_for_user(engine, user_id=b) as conn:
        result = await conn.execute(text(f"UPDATE {table} SET updated_at = 999"))
        assert result.rowcount == 0, f"RLS leak: UPDATE in {table} touched foreign rows"


@pytest.mark.parametrize("table,cols,values", PERSONAL_TABLES_WITH_INSERT)
async def test_user_b_cannot_delete_user_a_rows(engine, owner_engine, table, cols, values):
    async with tx_for_user(engine, user_id=b) as conn:
        result = await conn.execute(text(f"DELETE FROM {table}"))
        assert result.rowcount == 0, f"RLS leak: DELETE in {table} touched foreign rows"


async def test_app_role_cannot_bypass_rls_via_set_role(engine):
    async with engine.begin() as conn:
        with pytest.raises(Exception):
            # SET ROLE to a non-existent (or super) role must fail for holzi_app.
            await conn.execute(text("SET ROLE postgres"))


async def test_with_check_blocks_cross_user_write(engine, owner_engine):
    """WITH CHECK prevents user A from inserting a row owned by user B."""
    async with owner_engine.begin() as conn:
        a = (await conn.execute(text(
            "INSERT INTO users(email, role, bootstrap_completed, created_at) "
            "VALUES ('a@t', 'member', false, 0) RETURNING id"
        ))).first().id
        b = (await conn.execute(text(
            "INSERT INTO users(email, role, bootstrap_completed, created_at) "
            "VALUES ('b@t', 'member', false, 0) RETURNING id"
        ))).first().id
    async with tx_for_user(engine, user_id=a) as conn:
        with pytest.raises(Exception):
            await conn.execute(text(
                f"INSERT INTO notes(user_id, key, content, updated_at) "
                f"VALUES ({b}, 'k', 'c', 0)"
            ))
```

**Step 2: Run it**

```bash
uv run pytest tests/test_rls_cross_user.py -v
```
Expected: all pass. **A single failure here is a stop-the-world bug** — the §1 floor is broken.

**Step 3: Commit**

```bash
git add tests/test_rls_cross_user.py
git commit -m "test: RLS cross-user denial smoke test"
```

---

### Task 20: Fix the rest of the test suite + smoke-run

**Files:**
- Modify: every existing test under `tests/` that:
  - Reads `HERMES_DB_PATH` or `HERMES_AUTH_TOKEN` from env.
  - Imports the old `ensure_users_seeded` (now renamed).
  - Asserts SQLite-specific behavior (e.g. raw `PRAGMA` results, FTS5 syntax in expected queries).
  - Used the `conn` fixture (now `engine`).

**Step 1: Inventory**

```bash
grep -rln "HERMES_DB_PATH\|HERMES_AUTH_TOKEN\|ensure_users_seeded\|aiosqlite\|PRAGMA\|sqlite_insert\|messages_fts\|notes_fts" tests/ src/
```

**Step 2: Fix in waves**

Tackle one test file at a time. For each:
1. Change the env var name in fixtures.
2. Replace `conn` fixture parameter with `engine` or `owner_engine`.
3. Replace any direct `engine.begin()` test setup that wrote rows under a user with `tx_for_user(engine, user_id=…)`.

**Step 3: Smoke run**

```bash
uv run pytest -x
```
Expected: every test passes. Where one doesn't, fix it before continuing — do not gate any task here.

**Step 4: Commit per wave**

```bash
git add tests/test_<name>.py
git commit -m "test: port test_<name> to Postgres + RLS"
```

---

### Task 21: Lifespan boot smoke + end-to-end identity round-trip

**Files:**
- Create: `tests/test_lifespan_postgres.py`

**Step 1: Write the test**

```python
"""End-to-end smoke: boot the app against a per-test Postgres, hit a
trivial route as the env-seeded platform_admin, prove the request flowed
through:
  bearer → SessionResolver → ContextVar → tx_for_user → RLS-bound query.
"""
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.mark.usefixtures("pg_db")
async def test_platform_admin_authenticates_and_owns_a_persona(monkeypatch):
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_EMAIL", "admin@e2e.local")
    # Reload settings so the lifespan picks up the env.
    import importlib, hermes.config
    importlib.reload(hermes.config)

    from hermes.main import app
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(
                "/api/personas",
                headers={"Authorization": "Bearer test-admin-token"},
            )
    assert r.status_code == 200
    # Default persona was seeded by ensure_personas_backfill against user_id=admin.
    bodies = r.json()
    assert any(p.get("is_default") for p in bodies)
```

**Step 2: Run**

```bash
uv run pytest tests/test_lifespan_postgres.py -v
```
Expected: pass.

**Step 3: Commit**

```bash
git add tests/test_lifespan_postgres.py
git commit -m "test: lifespan + RLS end-to-end smoke"
```

---

### Task 22: Final cleanup + dependency / doc updates

**Files:**
- Modify: `README.md` — replace the "SQLite + FTS5" mentions with "Postgres + RLS".
- Modify: `Dockerfile` — drop any `apt-get install sqlite3`-shaped lines; ensure `postgresql-client` is present if any boot script uses `pg_isready`.
- Modify: `pyproject.toml` description string.

**Step 1: Skim and update wording**

`grep -rn "SQLite\|sqlite\|FTS5\|fts5" README.md docs/` — update the prose where it materially misleads. Don't rewrite the world.

**Step 2: Update `.env.example`**

Replace `HERMES_AUTH_TOKEN=…` and `HERMES_DB_PATH=…` with `HERMES_PLATFORM_ADMIN_TOKEN=…`, `HERMES_PLATFORM_ADMIN_EMAIL=…`, `HERMES_DATABASE_URL=postgresql+asyncpg://holzi_owner:…@db:5432/holzi`.

**Step 3: Mark Wave-C SQLite docs superseded**

Add a one-line banner at the top of any `docs/plans/2026-…-wave-c*.md` that this plan invalidates:
> **SUPERSEDED** by [`docs/plans/2026-06-11-saas-coding-agent-design.md`](2026-06-11-saas-coding-agent-design.md) §1 — SQLite framing replaced by Postgres + RLS. The structural decisions in this doc still apply.

**Step 4: Final test run**

```bash
uv run pytest -x -q
```
Expected: green.

**Step 5: Commit**

```bash
git add -A
git commit -m "docs: §1 done — Postgres + RLS foundation"
```

---

## Verification checklist (don't skip)

Run after Task 22:

- [ ] `uv run pytest -x` — green.
- [ ] `uv run pytest tests/test_rls_cross_user.py -v` — green. **Every** parametrized case passes; no warnings about empty result sets being mistaken for success.
- [ ] `docker compose -f docker-compose.local.yml exec db psql -U holzi_owner -d holzi -c "\dp"` — `holzi_app` shows `arwd` (insert/select/update/delete) on personal tables; no `*` (no GRANT OPTION); no role membership of `holzi_owner`.
- [ ] `docker compose -f docker-compose.local.yml exec db psql -U holzi_owner -d holzi -c "SELECT tablename, rowsecurity, forcerowsecurity FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"` — every personal table has both `t`.
- [ ] `docker compose -f docker-compose.local.yml exec db psql -U holzi_app -d holzi -c "SET ROLE postgres"` — error (`permission denied to set role`).
- [ ] `uv run alembic downgrade base && uv run alembic upgrade head` — clean down + up with no manual repair.
- [ ] Boot the app: `docker compose -f docker-compose.local.yml up hermes`, then `curl -H "Authorization: Bearer $HERMES_PLATFORM_ADMIN_TOKEN" http://localhost:8000/api/personas` returns 200.
- [ ] No file in `src/` still imports `aiosqlite`, references `PRAGMA`, or reads `schema.sql`.

---

## Notes for the executor

- **Bite-sized commits.** Each task above is one commit minimum. If a task feels like it has more than one logical change, split it.
- **Defense in depth.** Every repository function keeps its `WHERE user_id = …` filter after this plan. RLS is the floor, not a replacement for sensible queries.
- **The owner-engine escape hatch is small on purpose.** Only the lifespan bootstrap (`ensure_platform_admin_seeded`) and the global sweepers (`list_expired`, `list_due`) use `tx_as_owner`. If you find yourself reaching for it in a request-path code path, you're probably solving the wrong problem.
- **§2 is next.** Resist adding `org_id`, `audit_log`, `org_admin`, or the magic-link minter to §1 — they have their own plan.
- **If a test fails because RLS bites a legitimate operation,** the right fix is `user_id=…` on the query, not `tx_as_owner`. The few exceptions are listed above.
- **When migrating tests in Task 20,** if a test asserted SQLite-specific behavior that's irrelevant now (e.g. PRAGMA results, FTS5 syntax), delete it rather than port a meaningless assertion.

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-06-11-section-1-postgres-rls.md`. Two execution options:

1. **Subagent-Driven (this session)** — I dispatch fresh subagent per task, review between tasks, fast iteration.
2. **Parallel Session (separate)** — open new session with `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?

---

## Implementation log — Tasks 1–17 shipped (PR #87)

Branch: `feat/section-1-postgres-rls`. 34 commits on top of `cc1c268` (the plan doc commit).

### What shipped vs. the spec — known divergences

The implementation broadly matches the plan, but several pragmatic deviations were made during execution. Future tasks (18–22) need to be aware of them:

1. **Alembic `env.py` uses the canonical `_do_run_migrations(connection)` callback** instead of the plan's split-`run_sync` form (which had a double-transaction risk for Task 6's autogenerate). Plan §256 was updated in commit `c8a08ef`.
2. **`compare_type=True` + `compare_server_default=True`** added to `_do_run_migrations` to catch column-type swaps (`Integer→Boolean`). Plan updated.
3. **`set_current_user_token(user_id) -> Token` + `reset_current_user(token)`** API shipped in Task 10 (one task earlier than the plan called for). The plan's plain `set_current_user(...)` was unsafe across asyncio tasks (no Token returned). Task 12 just wired the existing helpers into the middleware.
4. **`tx_for_user` uses `SELECT set_config('app.user_id', :u, true)`** instead of `SET LOCAL app.user_id = :u` — asyncpg/Postgres rejects parameterised `SET LOCAL` ("syntax error at or near $1"). Same semantics (transaction-scoped via the `true` third arg). Discovered during the Task 17 lifespan smoke.
5. **`llm_credentials_user_active_uq`** (the per-user partial unique on `is_active=true`) is declared **both** in `schema.py` (`Index(..., postgresql_where=...)`) and in the `0001_initial.py` migration. Without the metadata declaration, every future `alembic check` would propose dropping it.
6. **Schema-port also flipped `personas.{soul,identity,agents}` + `persona_history.author` from `server_default=""` (Python str) to `server_default=sa_text("''")` / `sa_text("'user'")`** — purely DDL-stability (autogenerate-friendly), no runtime change.
7. **Task 11 changed `agent_tasks._get_unscoped` + `mark_run`** to use `tx_as_owner(owner_engine)` (the plan only listed `list_due`). Both legitimately query/mutate across users from the scheduler's perspective.
8. **Several "boolean compared to integer" bugs in repos fixed during Task 17's lifespan smoke** — `is_active == 1`, `enabled == 1`, `is_default == 1` in 5 repository modules. Now `.is_(True)` / `.is_(False)`. Task 11 ported the schema columns to Boolean but missed these SQLAlchemy comparison operators.
9. **`runs.aggregate_by_day::strftime` ported** to `func.to_char(func.to_timestamp(...), "YYYY-MM-DD")` as a bonus fix in the Task 13–15 batch (Postgres has no `strftime`).
10. **`docker compose -f docker-compose.yml down -v -p hermes-local`** is the documented volume-reset path in `docs/postgres-dev.md` (the original `docker volume rm holzi_holzi-pg` form had the wrong project name).
11. **Signal-cli orphan sweep** (commit `8a899d3`): dead `signal-cli-rest-api` service, `HERMES_SIGNAL_URL` env, `signal-data` volume, README "Linking Signal" section all removed during Task 1. Plan 34 had retired the messenger surface but the infra files still referenced it.

### Dead code awaiting cleanup (Task 20 or 22)

- `src/hermes/personas.py` still has `_migrate_prompt_to_fragments`, `_drop_persona_skills_table`, `_migrate_skills_add_enabled`, `_migrate_personas_add_credential_columns` — SQLite-PRAGMA helpers that the lifespan no longer calls. Function bodies left in place to keep Task 17's commit focused; delete in Task 20 or 22.
- `tests/conftest.py` still has the SQLite `conn` fixture and the SQLite-shaped `_TEST_DB_PATH` lifecycle. Task 18 replaces it wholesale.
- `tests/test_db.py` deleted in Task 16 (it asserted on `sqlite_master`, FTS5 virtual tables, and the deleted `init_db(path)` signature — 100% dead).
- ~10 test files reference the renamed `ensure_users_seeded` (now `ensure_platform_admin_seeded`) and old fixture names. Task 20 ports them.

### Remaining tasks

- **Task 18** — `conftest.py`: testcontainers-postgres, per-test DB, `engine`/`owner_engine`/`pg_db`/`seed_user`/`app_with_pg` fixtures.
- **Task 19** — `tests/test_rls_cross_user.py`: parametrized SELECT/UPDATE/DELETE denial for every personal table + `WITH CHECK` write-denial.
- **Task 20** — port the existing test suite (drop `conn` fixture, fix env, fix signature mismatches from Tasks 10/11/17). Delete dead `_migrate_*` helpers in personas.py while doing this.
- **Task 21** — `tests/test_lifespan_postgres.py`: bearer → ContextVar → tx_for_user → RLS-filtered query end-to-end.
- **Task 22** — README/Dockerfile cleanup, mark Wave-C docs superseded, final verification checklist.
