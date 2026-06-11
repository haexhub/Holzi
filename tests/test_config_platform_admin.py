import importlib
import os


def test_platform_admin_env_vars_required(monkeypatch):
    monkeypatch.delenv("HERMES_PLATFORM_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_AUTH_TOKEN", raising=False)
    import hermes.config as cfg

    # Without the admin token the Settings() construction must fail loudly.
    # `importlib.reload` also re-evaluates the module-level `settings = Settings()`,
    # which is where the missing-token validation actually fires today.
    try:
        importlib.reload(cfg)
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
