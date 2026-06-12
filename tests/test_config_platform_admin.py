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
