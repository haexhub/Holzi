from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Conversation:
    id: int
    channel: str
    external_id: str | None
    title: str | None
    started_at: int
    updated_at: int
    # Plan 35 §C1: owning user. Defaults to the seeded admin (id=1) so
    # background callers and legacy rows have a sensible owner.
    user_id: int = 1
    bookmarked: bool = False
    # unix epoch seconds; None means the conversation is bookmarked
    # (never expires).
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    conversation_id: int
    role: str
    content: str
    ts: int
    meta_json: str | None


@dataclass(frozen=True, slots=True)
class Attachment:
    id: int
    conversation_id: int
    # None while the upload is staged; set to the user message id at send.
    message_id: int | None
    filename: str
    content_type: str
    size: int
    # Opaque basename inside the conversation's attachments scratch dir.
    storage_path: str
    created_at: int


@dataclass(frozen=True, slots=True)
class Note:
    id: int
    key: str
    content: str
    tags: str | None
    updated_at: int
    # Plan 35 §C1: owning user. Defaults to the seeded admin (id=1) so
    # background callers and legacy rows have a sensible owner.
    user_id: int = 1


@dataclass(frozen=True, slots=True)
class AgentTask:
    """A scheduled or one-shot job that runs `prompt` through the agent loop.

    Exactly one of `due_at` / `schedule` is set: `due_at` is unix epoch
    seconds for a one-shot, `schedule` is a 5-field cron expression
    interpreted in `timezone` for a recurring task. `last_run_*` are
    populated by the scheduler after each firing.
    """

    id: int
    title: str
    prompt: str
    due_at: int | None
    schedule: str | None
    timezone: str
    enabled: bool
    last_run_at: int | None
    last_status: str | None
    last_run_id: str | None
    created_at: int
    updated_at: int
    # Plan 35 §C1: owning user. Defaults to the seeded admin (id=1) so
    # background callers and legacy rows have a sensible owner.
    user_id: int = 1


@dataclass(frozen=True, slots=True)
class LlmCredential:
    """A row from `llm_credentials`. Ciphertext columns stay raw — callers
    decide whether and when to decrypt (route handlers never decrypt; the
    agent loop and the proxy resolver do)."""

    id: int
    provider: str
    mode: str
    display_name: str
    base_url: str | None
    model: str | None
    is_active: bool
    api_key_iv: str | None
    api_key_tag: str | None
    api_key_data: str | None
    oauth_status: str | None
    oauth_authorized_at: int | None
    oauth_iv: str | None
    oauth_tag: str | None
    oauth_data: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class AgentRun:
    """A row from `agent_runs`. `error_*` columns are populated only when
    `status == 'error'`; `input_tokens`/`output_tokens` only when the
    upstream provider reported a usage block (some OpenAI-compatible
    streams omit it)."""

    id: str
    conversation_id: int
    channel: str
    model: str
    started_at: int
    finished_at: int | None
    status: str  # 'running' | 'success' | 'cancelled' | 'error'
    error_code: str | None
    error_message: str | None
    error_trace: str | None
    input_tokens: int | None
    output_tokens: int | None
    # Set when the scheduler triggered this run from an `agent_tasks` row
    # (Plan 16). NULL for plain /api/chat-driven runs.
    agent_task_id: int | None = None


@dataclass(frozen=True, slots=True)
class ToolApproval:
    """A row from `tool_approvals` — a tool the user has granted standing
    (`allow_always`) permission for. `last_used_at` is reserved for a future
    audit surface and stays None today."""

    tool_name: str
    granted_at: int
    last_used_at: int | None


@dataclass(frozen=True, slots=True)
class Workspace:
    """A row from `workspaces` (Plan 25). `id` is the stable kebab-case slug
    used as the workspace_id on `SandboxManager` and as the path component
    `${sandbox_volume_root}/${id}`. `archived_at` is None for active rows."""

    id: str
    display_name: str
    created_at: int
    archived_at: int | None


@dataclass(frozen=True, slots=True)
class Persona:
    """A row from `personas` (Plan 29-A → fragments per Plan 36).

    The original single `prompt` column was split into three typed
    fragments (`soul`, `identity`, `agents`); the resolver composes
    them at runtime. `is_default` is the global fallback persona; at
    most one row may have it set (DB trigger).
    """

    id: int
    name: str
    soul: str
    identity: str
    agents: str
    is_default: bool
    created_at: int
    updated_at: int
    llm_credential_id: int | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class PersonaHistory:
    """A row from `persona_history` (Plan 36).

    `snapshot_json` is the raw JSON string of
    `{name, soul, identity, agents}`; the repo layer parses it for
    callers that need fields. `author` is one of `'user'` (normal
    edits), `'system'` (initial seed via `ensure_backfill`) or
    `'migration'` (one-shot legacy → fragments migration); Wave C
    will swap user-tier writes for a real user_id.
    """

    id: int
    persona_id: int
    author: str
    snapshot_json: str
    created_at: int


@dataclass(frozen=True, slots=True)
class ChannelPromptRow:
    """A row from `channel_prompts` (Plan 29-A). One row per registered
    channel key — seeded idempotently on boot from
    `hermes.personas.CHANNEL_REGISTRY`. `default_persona_id` may be NULL
    (resolver then falls back to the globally-default persona)."""

    channel: str
    prompt: str
    default_persona_id: int | None
    updated_at: int


@dataclass(frozen=True, slots=True)
class Skill:
    """A row from `skills` (Plan 33 → Plan 37).

    The `description` / `when_to_use` fields are the Anthropic-skill
    frontmatter promoted to columns; `body_markdown` is the prompt
    content loaded via `skill_load`. `enabled=False` means the skill
    is invisible to the agent (not in the catalog index, not loadable
    via `skill_load`) but still editable via the UI.
    """

    id: int
    slug: str
    name: str
    description: str
    when_to_use: str
    body_markdown: str
    enabled: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class McpServer:
    """A row from `mcp_servers` (Plan 32).

    `env_keys` is the projection of the stored JSON env-map down to just
    the variable names — values may be secrets and are never returned to
    callers. `credentials_*` are the raw AES-GCM ciphertext tripel; the
    manager decrypts when it constructs the per-transport client. Route
    layer never decrypts and never exposes the plaintext value.
    """

    id: int
    name: str
    display_name: str
    transport: str  # 'http' | 'stdio'
    url: str | None
    command_argv: list[str] | None
    env_keys: list[str]
    credentials_iv: str | None
    credentials_tag: str | None
    credentials_data: str | None
    enabled: bool
    last_error: str | None
    created_at: int
    updated_at: int


