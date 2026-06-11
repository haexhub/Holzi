from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Dev-only password baked in; production MUST override via HERMES_DATABASE_URL.
    database_url: str = (
        "postgresql+asyncpg://holzi_owner:holzi_owner_dev_pw@db:5432/holzi"
    )

    # Runtime DSN: the app connects as holzi_app, NOT as the migration owner.
    # When unset, derived from database_url by substituting the role + password.
    # Production MUST override `runtime_role_password` via HERMES_RUNTIME_ROLE_PASSWORD.
    runtime_database_url: str | None = None
    runtime_role_password: str = "holzi_app_dev_pw"

    log_level: str = "INFO"
    # Plan 27: when set, structlog also writes JSON rows to this file
    # (rotated by `RotatingFileHandler`) so the Control Center's Logs page
    # has something to tail. Unset = stdout-only; the /api/logs endpoint
    # reports 503 in that case.
    log_file: str | None = None
    # Bounds matter: max_bytes <= 0 silently disables rollover in
    # RotatingFileHandler, which converts the file handler into an
    # unbounded sink. Fail fast on a bad .env value instead.
    log_file_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    log_file_backup_count: int = Field(default=3, ge=0)
    # AES-256-GCM master key for `llm_credentials` ciphertext. Optional —
    # when unset, `crypto.resolve_master_key` falls back to a persisted
    # keyfile next to the data dir (auto-generated on first run, mode 0600).
    # Set explicitly when running multiple Hermes processes against a
    # shared DB (e.g. with the haex-claude-proxy sqlite-resolver).
    secret_key: str | None = None
    # Upstream LLM endpoint. Anything that speaks the OpenAI
    # /v1/chat/completions format works: haex-claude-proxy (the default,
    # Claude Max), OpenAI direct, OpenRouter, Ollama, a LiteLLM proxy, ...
    llm_url: str = "http://haex-claude-proxy:8080"
    llm_api_key: str = ""
    model: str = "claude-opus-4-7"
    brave_api_key: str = ""
    # Default conversation TTL — 30 days from the last user message.
    # Bookmarked conversations override this (expires_at = NULL).
    conversation_ttl_days: int = 30
    # Root for per-conversation scratch directories
    # (`{data_dir}/conversations/{id}/`). Defaults to cwd so backups can
    # be configured explicitly per-deployment.
    data_dir: str | None = None
    # --- Sandbox runtime (Plan 11b-a) ---------------------------------------
    # Rootless Podman, Docker-API-compatible socket. When the socket is unset
    # the agent boots without a sandbox manager (tests, sandbox-less deploys);
    # any tool that needs a sandbox fails loudly rather than running in-process.
    sandbox_socket: str = ""
    # Matches `make sandbox-image` / the compose default tag.
    sandbox_image: str = "hermes-sandbox:dev"
    # Sandbox network. Default "none" = no networking at all: the agent drives
    # sandboxes over the Podman control socket (exec), not the network, so a
    # 11b-a sandbox needs no network and thus cannot reach the agent's DB,
    # secrets, or other sandboxes. (Separate Podman networks are NOT isolated
    # from each other by default — verified — so "own network" isn't enough.)
    # Controlled egress for git/builds is a deliberate later addition (13/16).
    sandbox_network: str = "none"
    # Mandatory per-container caps. No "unlimited" path exists.
    # CPU + memory are hard caps (the host's rootless cgroup v2 must delegate
    # the cpu + memory controllers — `Delegate=cpu cpuset memory pids`).
    sandbox_cpus: float = 1.0
    sandbox_memory_mb: int = 1024
    sandbox_disk_mb: int = 2048
    # Disk quota is best-effort: overlay `StorageOpt size` only works on an
    # XFS-backed store with pquota (ext4/btrfs reject it and the create fails).
    # Off by default so the agent runs on any host; enable on XFS-backed
    # production storage to actually cap sandbox disk usage.
    sandbox_disk_quota: bool = False
    # --- Workspace browser (Plan 12) ---------------------------------------
    # Comma-separated workspace ids the read-only browser exposes. Empty by
    # default — the API still answers but reports an empty root list, which
    # the frontend renders as "no workspaces configured". Each id is a
    # SandboxManager workspace key; tree/file reads spin the sandbox up on
    # first use via the existing `get_workspace` semantics.
    workspace_roots: str = ""
    # --- Workspace git (Plan 24) -------------------------------------------
    # Gates the *destructive* git endpoints (discard, hard reset). Off by
    # default so a misconfigured deployment can never throw away unstaged
    # work via an API call; flip to true on dev hosts where the workspace
    # is checkpointed externally (snapshot, separate backup branch).
    workspace_git_destructive: bool = False


# Constructed at import time so missing env vars fail boot, not first DB call.
# Tests that need to probe the validation path (missing required fields) use
# `importlib.reload(hermes.config)` — see tests/test_config_platform_admin.py.
settings = Settings()  # type: ignore[call-arg]


def get_data_dir() -> Path:
    """Resolve the on-disk data root for non-DB state (master key,
    scratch directories). Honours `HERMES_DATA_DIR`; falls back to cwd
    when unset (tests + dev). Production deployments set HERMES_DATA_DIR
    explicitly so backups capture the right directory.
    """
    if settings.data_dir:
        return Path(settings.data_dir)
    return Path.cwd()


def conversation_scratch_root() -> Path:
    """Where per-conversation scratch directories live."""
    return get_data_dir() / "conversations"
