from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HERMES_",
        env_file=".env",
        extra="ignore",
    )

    auth_token: str = Field(..., min_length=1)
    log_level: str = "INFO"
    db_path: str = "./hermes.db"
    # AES-256-GCM master key for `llm_credentials` ciphertext. Optional —
    # when unset, `crypto.resolve_master_key` falls back to a persisted
    # keyfile next to the DB (auto-generated on first run, mode 0600).
    # Set explicitly when running multiple Hermes processes against a
    # shared DB (e.g. with the haex-claude-proxy sqlite-resolver).
    secret_key: str | None = None
    # Upstream LLM endpoint. Anything that speaks the OpenAI
    # /v1/chat/completions format works: haex-claude-proxy (the default,
    # Claude Max), OpenAI direct, OpenRouter, Ollama, a LiteLLM proxy, ...
    llm_url: str = "http://haex-claude-proxy:8080"
    llm_api_key: str = ""
    signal_url: str = "http://signal-cli-rest-api:8080"
    signal_number: str = ""
    model: str = "claude-opus-4-7"
    brave_api_key: str = ""
    # Default conversation TTL — 30 days from the last user message.
    # Bookmarked conversations override this (expires_at = NULL).
    conversation_ttl_days: int = 30
    # Root for per-conversation scratch directories
    # (`{data_dir}/conversations/{id}/`). Defaults to the db_path's
    # parent so backups capture the DB + scratch together.
    data_dir: str | None = None
    # --- Sandbox runtime (Plan 11b-a) ---------------------------------------
    # Rootless Podman, Docker-API-compatible socket. When the socket is unset
    # the agent boots without a sandbox manager (tests, sandbox-less deploys);
    # any tool that needs a sandbox fails loudly rather than running in-process.
    sandbox_socket: str = ""
    # Matches `make sandbox-image` / the compose default tag.
    sandbox_image: str = "hermes-sandbox:dev"
    # Dedicated, locked-down network. Sandboxes attach here and nowhere else —
    # never the agent's `internal` network (DB, secrets, other services).
    sandbox_network: str = "hermes-sandbox"
    # Mandatory per-container caps. No "unlimited" path exists.
    sandbox_cpus: float = 1.0
    sandbox_memory_mb: int = 1024
    sandbox_disk_mb: int = 2048


settings = Settings()  # type: ignore[call-arg]


def get_data_dir() -> Path:
    """Resolve the on-disk data root for non-DB state (master key,
    scratch directories). Honours `HERMES_DATA_DIR`; falls back to the
    DB file's parent so a single backup of one directory covers both.
    Pure :memory: deployments (tests) get cwd as a last resort.
    """
    if settings.data_dir:
        return Path(settings.data_dir)
    if settings.db_path == ":memory:":
        return Path.cwd()
    return Path(settings.db_path).resolve().parent


def conversation_scratch_root() -> Path:
    """Where per-conversation scratch directories live."""
    return get_data_dir() / "conversations"
