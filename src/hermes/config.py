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
    proxy_url: str = "http://haex-claude-proxy:8080"


settings = Settings()  # type: ignore[call-arg]
