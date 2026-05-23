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


settings = Settings()  # type: ignore[call-arg]
