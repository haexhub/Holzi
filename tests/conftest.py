import os
import tempfile

# Tests need a file-based SQLite path (not :memory:) so the default
# AsyncAdaptedQueuePool can hand out one connection per concurrent task.
# With :memory: + StaticPool, the reminder scheduler's background loop
# would share a single connection with whatever the test is doing and
# race on transaction state.
_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix=".db", prefix="hermes-test-")
os.close(_TEST_DB_FD)
os.unlink(_TEST_DB_PATH)  # init_db will recreate; we just wanted a unique path

os.environ.setdefault("HERMES_AUTH_TOKEN", "test-token-for-pytest")
os.environ.setdefault("HERMES_LOG_LEVEL", "WARNING")
os.environ.setdefault("HERMES_DB_PATH", _TEST_DB_PATH)

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from hermes import config as hermes_config  # noqa: E402
from hermes.db import init_db  # noqa: E402


@pytest.fixture
async def conn(tmp_path: Path):
    """Yields an AsyncEngine bound to a fresh per-test SQLite DB.

    Fixture name stayed `conn` for diff-minimisation across the test
    suite during the SQLAlchemy refactor — repo functions take an engine
    now, so all callsites compile, just with a slightly misleading name.

    Seeds the admin user (id=1) so repo-level tests can create
    conversations owned by user 1 without tripping the `user_id` foreign
    key (FK enforcement is ON for every connection). Mirrors what the
    production lifespan does via `ensure_users_seeded`.
    """
    engine = await init_db(str(tmp_path / "hermes.db"))
    from hermes.users import ensure_users_seeded

    await ensure_users_seeded(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_app_db_path(monkeypatch, tmp_path: Path) -> None:
    """Force each integration test (anything using LifespanManager) to boot
    against a fresh file-based DB. Without this the lifespan would re-use
    the module-level `_TEST_DB_PATH` between tests and leak state.
    """
    fresh = str(tmp_path / "hermes.db")
    monkeypatch.setattr(hermes_config.settings, "db_path", fresh)


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
