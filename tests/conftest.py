import os

os.environ.setdefault("HERMES_PLATFORM_ADMIN_TOKEN", "test-token-for-pytest")
os.environ.setdefault("HERMES_PLATFORM_ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("HERMES_LOG_LEVEL", "WARNING")
# DATABASE_URL is provided per-test by the testcontainers fixture (Task 18).

import secrets  # noqa: E402

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from alembic import command  # noqa: E402
from hermes.config import settings  # noqa: E402

# --- testcontainers Postgres -------------------------------------------------
#
# The shipped migrations hardcode the database name `holzi` (0002:
# `GRANT CONNECT ON DATABASE holzi`, 0003: `ALTER DATABASE holzi SET app.user_id`)
# and the holzi_app password `holzi_app_dev_pw` (0002 CREATE ROLE). So the test
# container's database MUST be named `holzi`, and the app DSN MUST use that
# password — otherwise migrations raise `database "holzi" does not exist` or the
# app engine fails authentication.
#
# Roles are cluster-global and the db name is fixed, so rather than create/drop
# a database per test we boot ONE session container named `holzi`, migrate it
# once, and isolate per test by TRUNCATE ... RESTART IDENTITY CASCADE (as owner,
# which RLS does not apply to). RESTART IDENTITY resets sequences so the
# freshly-seeded platform_admin is always user_id=1.

_OWNER_USER = "holzi_owner"
_OWNER_PASSWORD = "holzi_owner_test_pw"
# holzi_app's password is baked into migration 0002 — do not change it here.
_APP_USER = "holzi_app"
_APP_PASSWORD = "holzi_app_dev_pw"
_DB_NAME = "holzi"


def _dsn(user: str, password: str, host: str, port: int) -> str:
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{_DB_NAME}"


@pytest.fixture(scope="session")
def _pg_container():
    """Session-scoped Postgres container. Sync fixture: testcontainers is sync
    and needs no event loop. The database is named `holzi` (migrations require
    it). Yields a dict of the owner + app asyncpg DSNs."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine",
        username=_OWNER_USER,
        password=_OWNER_PASSWORD,
        dbname=_DB_NAME,
    ) as pg:
        host = pg.get_container_host_ip()
        port = int(pg.get_exposed_port(5432))
        yield {
            "owner_url": _dsn(_OWNER_USER, _OWNER_PASSWORD, host, port),
            "app_url": _dsn(_APP_USER, _APP_PASSWORD, host, port),
        }


@pytest.fixture(scope="session")
def _migrated_pg(_pg_container):
    """Run Alembic to head exactly once against the owner DSN. Sync fixture:
    `alembic/env.py` does `asyncio.run(run_migrations_online())`, which requires
    NO running loop — a sync fixture has none.

    Creates the schema, the holzi_app role (password holzi_app_dev_pw), the RLS
    policies, and the `ALTER DATABASE holzi SET app.user_id TO '0'` GUC.
    """
    owner_url = _pg_container["owner_url"]
    # Mutate the singleton in place for the upgrade ONLY — do NOT importlib.reload
    # hermes.config (that rebinds a new object db.py/main.py never see). env.py
    # forces sqlalchemy.url := settings.database_url at command.upgrade time, so
    # the cfg.set_main_option below is redundant; what actually matters is the
    # singleton value. Restore it afterwards so this session-scoped fixture does
    # not leak the container DSN onto the global singleton for tests that don't
    # use pg_db — pg_db re-pins it per test via auto-restoring monkeypatch.
    prev_database_url = settings.database_url
    settings.database_url = owner_url
    try:
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", owner_url)
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = prev_database_url

    return _pg_container


@pytest.fixture
async def pg_db(_migrated_pg, monkeypatch):
    """Per-test clean Postgres. Points the config singleton (and env, belt-and-
    suspenders) at the migrated `holzi` database, then truncates every table for
    a clean slate. Yields the owner + app DSNs."""
    owner_url = _migrated_pg["owner_url"]
    app_url = _migrated_pg["app_url"]

    monkeypatch.setattr(settings, "database_url", owner_url)
    monkeypatch.setattr(settings, "runtime_database_url", app_url)
    monkeypatch.setenv("HERMES_DATABASE_URL", owner_url)
    monkeypatch.setenv("HERMES_RUNTIME_DATABASE_URL", app_url)

    # The dev .env ships an invalid HERMES_SECRET_KEY (`dev-secret-key-not-real`),
    # which crypto.resolve_master_key rejects (needs 64 hex chars) and would
    # crash the app_with_pg lifespan boot. Pin a deterministic valid 32-byte
    # key so the full lifespan boots without depending on the local .env or
    # writing a keyfile into the worktree.
    monkeypatch.setattr(settings, "secret_key", "00" * 32)
    monkeypatch.setenv("HERMES_SECRET_KEY", "00" * 32)

    # Truncate as owner — RLS does not apply to TRUNCATE, and owner owns the
    # tables. RESTART IDENTITY resets sequences so a freshly-seeded admin is
    # id=1 again. CASCADE follows FKs so we don't have to order the tables.
    truncate_engine = create_async_engine(owner_url, pool_pre_ping=True)
    try:
        async with truncate_engine.begin() as conn:
            rows = (await conn.execute(text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename <> 'alembic_version'"
            ))).all()
            tables = [r.tablename for r in rows]
            if tables:
                joined = ", ".join(f'"{t}"' for t in tables)
                await conn.execute(text(
                    f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"
                ))
    finally:
        await truncate_engine.dispose()

    yield {"owner_url": owner_url, "app_url": app_url}


@pytest.fixture
async def engine(pg_db):
    """holzi_app engine — subject to RLS (NOBYPASSRLS). The per-request engine."""
    eng = create_async_engine(pg_db["app_url"], pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def owner_engine(pg_db):
    """holzi_owner engine — used to seed rows bypassing RLS in test setup."""
    eng = create_async_engine(pg_db["owner_url"], pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def seed_user(owner_engine):
    """Insert a `member` user and return its id. Email is unique per call so
    tests can request more than one."""
    email = f"u_{secrets.token_hex(4)}@test.local"
    async with owner_engine.begin() as conn:
        row = (await conn.execute(
            text(
                "INSERT INTO users(email, role, bootstrap_completed, created_at) "
                "VALUES (:e, 'member', false, 0) RETURNING id"
            ),
            {"e": email},
        )).first()
    return row.id


@pytest.fixture
async def conn(engine, owner_engine):
    """Compatibility shim for repo-level tests written against the old SQLite
    `conn` fixture: yields the holzi_app (RLS-bound) engine with a user id=1
    pre-seeded, mirroring what the old fixture did via ensure_users_seeded.
    Repo functions called with user_id=1 then work under RLS (tx_for_user sets
    app.user_id=1). Seed via owner_engine because `users` has no RLS but the row
    must exist before personal-table inserts (FK)."""
    async with owner_engine.begin() as c:
        await c.execute(text(
            "INSERT INTO users(email, role, bootstrap_completed, created_at) "
            "VALUES ('admin@test.local','platform_admin',false,0)"
        ))
    yield engine


@pytest.fixture
async def app_with_pg(pg_db):
    """Boot the full app lifespan against the per-test Postgres.

    `pg_db` already pointed `settings` at the container, so the lifespan's
    `init_db()` + `ensure_platform_admin_seeded` run against it — seeding the
    platform_admin as user_id=1 (fresh truncated DB) with a session keyed on
    the conftest-level HERMES_PLATFORM_ADMIN_TOKEN (`test-token-for-pytest`).
    """
    from asgi_lifespan import LifespanManager

    from hermes.db import get_current_user
    from hermes.main import app

    # Idempotently register a test probe route that echoes the current-user
    # ContextVar populated by the auth middleware.
    if not any(getattr(r, "path", None) == "/__test/whoami" for r in app.routes):
        async def _whoami():
            return {"user_id": get_current_user()}

        app.add_api_route("/__test/whoami", _whoami, methods=["GET"])

    async with LifespanManager(app):
        yield app


@pytest.fixture
async def client(app_with_pg):
    """httpx AsyncClient bound to the lifespan-booted app via ASGITransport."""
    import httpx

    transport = httpx.ASGITransport(app=app_with_pg)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def _empty_capability_index(monkeypatch) -> None:
    """Globally pin the capability index to empty for the test suite.

    `get_effective_system_prompt` reads the on-disk capability index and
    injects it between persona and channel. Tests across the suite assert
    on the exact composed string, so we pin the index loader to empty by
    default — the file's content otherwise leaks into every prompt
    assertion. Tests that specifically want to verify injection re-patch
    `capabilities.load_capability_index` with a non-empty value.
    """
    from hermes import capabilities

    monkeypatch.setattr(capabilities, "load_capability_index", lambda: "")


@pytest.fixture(autouse=True)
def _patch_persona_context_for_app_tests(request, monkeypatch) -> None:
    """Restore the pre-Task-6 upstream behavior for integration tests.

    `_stream_web_agent_run` now calls `resolve_persona_context` (which
    requires an active credential in the DB) and `build_client_for_credential`
    (which creates a fresh httpx client). Integration tests that call
    `/api/chat` rely on `app.state.upstream` being the mock transport,
    and they run against a fresh DB that has no credentials.

    This fixture:
    1. Patches `resolve_persona_context` in `routes.api` to compose the
       system prompt via `get_effective_system_prompt` (same as before) and
       return a sentinel credential so `build_client_for_credential` is called.
    2. Patches `build_client_for_credential` in `routes.api` to return
       `app.state.upstream` (the mock the test installs) rather than
       building a real credential-bound client.

    Tests that specifically exercise the credential-routing path (e.g.,
    `test_api_chat_uses_active_credential_model`) opt out by marking themselves
    with `@pytest.mark.real_persona_context`. Those tests seed their own
    credentials and verify end-to-end credential routing.

    Tests that exercise `hermes.personas.resolve_persona_context` directly
    (e.g., `test_personas_resolver.py`) import from the original module,
    not from `routes.api`, so they are unaffected by this patch.
    """
    import hermes.routes.api as api_mod
    from hermes.main import app
    from hermes.personas import PersonaContext, get_effective_system_prompt
    from hermes.repository.models import LlmCredential

    # `build_client_for_credential` always returns the test mock so tests that
    # set `app.state.upstream` don't need to worry about credential-bound
    # client creation. This patch applies to ALL integration tests.
    def _fake_build_client(cred, **kwargs):
        return app.state.upstream

    monkeypatch.setattr(api_mod, "build_client_for_credential", _fake_build_client)

    # `resolve_persona_context` requires an active credential in the DB.
    # Tests marked `real_persona_context` seed their own credential and opt
    # out of the fake resolver so the real credential-routing path is exercised.
    if request.node.get_closest_marker("real_persona_context"):
        return

    _SENTINEL_CRED = LlmCredential(
        id=0,
        provider="anthropic",
        mode="oauth_claude",
        display_name="test-sentinel",
        base_url=None,
        model=None,
        is_active=True,
        api_key_iv=None,
        api_key_tag=None,
        api_key_data=None,
        oauth_status=None,
        oauth_authorized_at=None,
        oauth_iv=None,
        oauth_tag=None,
        oauth_data=None,
        created_at=0,
        updated_at=0,
    )

    async def _fake_resolve_persona_context(
        channel: str,
        engine,
        *,
        user_id: int,
        model_override: str | None = None,
        persona_id_override: int | None = None,
    ) -> PersonaContext:
        from fastapi import HTTPException

        from hermes.config import settings
        from hermes.errors import ErrorCode
        from hermes.repository import personas as personas_repo

        if persona_id_override is not None:
            p = await personas_repo.get(engine, persona_id_override, user_id=user_id)
            if p is None:
                raise HTTPException(
                    status_code=404, detail=ErrorCode.PERSONA_NOT_FOUND.value
                )

        system_prompt = await get_effective_system_prompt(
            channel, engine, user_id=user_id
        )
        model = model_override or settings.model
        return PersonaContext(
            system_prompt=system_prompt,
            credential=_SENTINEL_CRED,
            model=model,
        )

    monkeypatch.setattr(api_mod, "resolve_persona_context", _fake_resolve_persona_context)

    async def _fake_resolve_chat_context_meta(channel: str, engine, *, user_id: int):
        from hermes.config import settings
        from hermes.repository import personas as personas_repo

        row = await personas_repo.get_default(engine, user_id=user_id)
        model = settings.model
        return (
            row.id if row else None,
            row.name if row else None,
            model,
        )

    monkeypatch.setattr(api_mod, "resolve_chat_context_meta", _fake_resolve_chat_context_meta)
