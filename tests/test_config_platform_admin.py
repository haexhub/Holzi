import importlib

import pytest
from pydantic import ValidationError


def test_platform_admin_env_vars_required(monkeypatch):
    monkeypatch.delenv("HERMES_PLATFORM_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_AUTH_TOKEN", raising=False)
    import hermes.config as cfg

    # Without the admin token, Settings() construction must fail with a
    # pydantic ValidationError that explicitly names the missing field.
    # `importlib.reload` re-evaluates the module-level `settings = Settings()`,
    # which is where the validation actually fires.
    with pytest.raises(ValidationError) as excinfo:
        importlib.reload(cfg)
    assert "platform_admin_token" in str(excinfo.value).lower()


def test_database_url_default(monkeypatch):
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "x")
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.delenv("HERMES_DATABASE_URL", raising=False)
    import hermes.config as cfg
    importlib.reload(cfg)
    s = cfg.Settings()  # type: ignore[call-arg]
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_runtime_role_password_required(monkeypatch):
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_TOKEN", "x")
    monkeypatch.setenv("HERMES_PLATFORM_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.delenv("HERMES_RUNTIME_ROLE_PASSWORD", raising=False)
    import hermes.config as cfg

    # No baked-in default: a missing role password must fail boot loudly,
    # naming the field — same contract as the platform-admin token above.
    with pytest.raises(ValidationError) as excinfo:
        importlib.reload(cfg)
    assert "runtime_role_password" in str(excinfo.value).lower()
