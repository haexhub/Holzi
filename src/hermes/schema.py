"""SQLAlchemy Core table definitions (Postgres-portable).

The non-virtual tables live here as `Table` objects. FTS5 virtual tables /
their sync triggers are SQLite-only and no longer modelled here — Postgres
full-text search is handled via `tsvector` columns in Alembic migrations.

Notes on portability:
- Boolean flags use `Boolean` (not Integer/0/1) so Postgres stores real bools
  and the API layer sees `True`/`False` naturally.
- Unix-epoch-seconds columns are kept as `Integer` for now. They are portable
  across SQLite and Postgres; §2 of the SaaS plan revisits whether to migrate
  them to `TIMESTAMPTZ`.
- Server-side defaults use `text()` SQL fragments (e.g. `text("false")`,
  `text("'UTC'")`) which both backends accept.
"""
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text as sa_text,
)

metadata = MetaData()


conversations = Table(
    "conversations",
    metadata,
    Column("id", Integer, primary_key=True),
    # 'web' | 'task' (extend via hermes.personas.CHANNEL_REGISTRY)
    Column("channel", Text, nullable=False),
    Column("external_id", Text),
    Column("title", Text),
    # unix epoch seconds (Integer for now — see module docstring)
    Column("started_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    # Pinned threads survive the daily TTL sweep.
    Column("bookmarked", Boolean, nullable=False, server_default=sa_text("false")),
    # unix epoch seconds; NULL means the conversation is bookmarked
    # (never expires). Refreshed whenever `updated_at` moves.
    Column("expires_at", Integer),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
)

Index(
    "conv_channel_updated",
    conversations.c.channel,
    conversations.c.updated_at.desc(),
)
Index(
    "conv_user_updated",
    conversations.c.user_id,
    conversations.c.updated_at.desc(),
)


messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "conversation_id",
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # 'user' | 'assistant' | 'tool'
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("ts", Integer, nullable=False),
    # optional JSON blob — tool_call_id, tool name, model, tokens, etc.
    Column("meta_json", Text),
    # Denormalized from conversations.user_id so RLS policies can scope
    # without a join (Plan §1).
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
)

Index("msg_conv_ts", messages.c.conversation_id, messages.c.ts)
Index("messages_user_ts", messages.c.user_id, messages.c.ts.desc())


attachments = Table(
    "attachments",
    metadata,
    Column("id", Integer, primary_key=True),
    # CASCADE on conversation delete: an attachment only lives inside its
    # conversation's scratch dir, which is rmtree'd on delete (Plan 01b).
    # The DB row goes with it so no orphan metadata survives.
    Column(
        "conversation_id",
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # NULL while the upload is staged (uploaded, not yet sent). Set to the
    # user message id when /api/chat links the attachment at send time.
    Column(
        "message_id",
        Integer,
        ForeignKey("messages.id", ondelete="CASCADE"),
    ),
    # Sanitised original filename — display only; the on-disk name is an
    # opaque token (storage_path) so user input can never drive the path.
    Column("filename", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    # Size in bytes.
    Column("size", Integer, nullable=False),
    # Opaque basename inside {scratch}/conversations/{id}/attachments/. The
    # absolute path is reconstructed from config so a relocated data_dir
    # (backup/restore) doesn't strand the files.
    Column("storage_path", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
    # Denormalized from conversations.user_id for RLS (Plan §1).
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
)

Index("attachments_conversation", attachments.c.conversation_id)
Index("attachments_message", attachments.c.message_id)
Index("attachments_user", attachments.c.user_id)


notes = Table(
    "notes",
    metadata,
    Column("id", Integer, primary_key=True),
    # `key` is unique PER USER (not globally) — see the UniqueConstraint below.
    Column("key", Text, nullable=False),
    Column("content", Text, nullable=False),
    # comma-separated tags — YAGNI on a join table
    Column("tags", Text),
    Column("updated_at", Integer, nullable=False),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    UniqueConstraint("user_id", "key", name="notes_user_key"),
)

Index("notes_tags", notes.c.tags)
Index("notes_user", notes.c.user_id)


# Scheduled and one-shot agent runs (Plan 16). Two shapes:
#   - One-shot: `schedule = NULL`, `due_at` is the firing time. After
#     `mark_run` records the firing, `enabled` flips to false; the row stays so
#     the user can still see its history. `due_at` is not cleared.
#   - Recurring: `schedule` is a 5-field cron expression interpreted in
#     `timezone`. `due_at` carries the *materialised* next firing — computed
#     at create time and rolled forward by `mark_run` after each tick — so
#     the scheduler's "enabled AND due_at <= now" query stays cheap and
#     index-friendly (no cron eval in the hot path).
# Invariant `due_at IS NOT NULL` is enforced at the repository layer rather
# than via a DB CHECK so we can evolve the trigger shape (e.g. interval, ical)
# without a migration dance. `last_run_id` is a loose pointer at the agent_runs
# row the scheduler produced last (NULL until the first firing).
agent_tasks = Table(
    "agent_tasks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", Text, nullable=False),
    Column("prompt", Text, nullable=False),
    # unix epoch seconds for one-shot; NULL for recurring.
    Column("due_at", Integer),
    # 5-field cron string for recurring; NULL for one-shot.
    Column("schedule", Text),
    # IANA tz name. Defaults to UTC so callers that don't care don't have to
    # think about it; recurring cron evaluation honours this.
    Column("timezone", Text, nullable=False, server_default=sa_text("'UTC'")),
    # One-shot tasks auto-flip to false after their single firing; the
    # pause/resume endpoint toggles this for recurring ones.
    Column("enabled", Boolean, nullable=False, server_default=sa_text("true")),
    Column("last_run_at", Integer),
    # 'success' | 'cancelled' | 'error' | 'running' — mirrors agent_runs.status.
    Column("last_status", Text),
    # Loose reference to agent_runs.id — no FK constraint to avoid a cyclic
    # schema with agent_runs.agent_task_id. The scheduler clears this field
    # when the underlying run row is gone; a stale id is harmless.
    Column("last_run_id", Text),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
)

# Hot-path for the scheduler tick: every poll we ask "what's enabled and due?".
# The single scheduler serves every user, so `list_due` stays GLOBAL and uses
# this index (no user_id leading column).
Index(
    "agent_tasks_enabled_due",
    agent_tasks.c.enabled,
    agent_tasks.c.due_at,
)
Index(
    "agent_tasks_user_enabled_due",
    agent_tasks.c.user_id,
    agent_tasks.c.enabled,
    agent_tasks.c.due_at,
)


# AES-GCM-encrypted credentials for outgoing LLM calls. `provider` chooses
# the API flavour, `mode` whether we forward a static API key or run the
# Claude OAuth subprocess. At most one row per user is active at a time
# (partial unique index declared in Alembic).
llm_credentials = Table(
    "llm_credentials",
    metadata,
    Column("id", Integer, primary_key=True),
    # 'anthropic' | 'openai' | 'openrouter' | 'google' | 'custom'
    Column("provider", Text, nullable=False),
    # 'api_key' | 'oauth_claude'
    Column("mode", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    # Optional override for OpenAI-compatible endpoints (e.g. self-hosted
    # OpenRouter mirror). NULL = use the provider's well-known base URL.
    Column("base_url", Text),
    # Preferred model for this credential — NULL = agent loop falls back
    # to settings.model.
    Column("model", Text),
    # At most one row per user may have is_active=true (partial unique
    # index declared in Alembic, scoped to user_id).
    Column("is_active", Boolean, nullable=False, server_default=sa_text("false")),
    # api_key mode ciphertext. Hex strings to keep the schema text-only.
    Column("api_key_iv", Text),
    Column("api_key_tag", Text),
    Column("api_key_data", Text),
    # oauth_claude mode: 'pending' | 'authorized' | 'expired'
    Column("oauth_status", Text),
    Column("oauth_authorized_at", Integer),
    Column("oauth_iv", Text),
    Column("oauth_tag", Text),
    Column("oauth_data", Text),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
)

Index("llm_credentials_user", llm_credentials.c.user_id)
# At most one active credential per user. Declared here (not just in Alembic)
# so `alembic check` / future autogenerate runs see the index in metadata and
# don't propose dropping it.
Index(
    "llm_credentials_user_active_uq",
    llm_credentials.c.user_id,
    unique=True,
    postgresql_where=sa_text("is_active = true"),
)


# Persistent chat-run history. One row per /api/chat call (or per task
# scheduler firing). Failure rows carry enough context (code + message +
# trace) for a "Recent failures" panel and post-hoc debugging without
# tailing container logs.
agent_runs = Table(
    "agent_runs",
    metadata,
    # run_id (uuid hex) — same id surfaced via the SSE `run` event so
    # frontend can correlate the row with the live stream.
    Column("id", Text, primary_key=True),
    Column(
        "conversation_id",
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # 'web' | 'task' (see hermes.personas.CHANNEL_REGISTRY) — kept as Text.
    Column("channel", Text, nullable=False),
    Column("model", Text, nullable=False),
    # unix epoch seconds.
    Column("started_at", Integer, nullable=False),
    # NULL while the run is still in flight; filled in by the finalizer.
    Column("finished_at", Integer),
    # 'running' | 'success' | 'cancelled' | 'error'.
    Column("status", Text, nullable=False),
    Column("error_code", Text),
    Column("error_message", Text),
    Column("error_trace", Text),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column(
        "agent_task_id",
        Integer,
        ForeignKey("agent_tasks.id", ondelete="SET NULL"),
    ),
    # Denormalized from conversations.user_id for RLS (Plan §1).
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
)

Index(
    "agent_runs_conv_started",
    agent_runs.c.conversation_id,
    agent_runs.c.started_at.desc(),
)
Index("agent_runs_status_started", agent_runs.c.status, agent_runs.c.started_at.desc())
Index(
    "agent_runs_user_started",
    agent_runs.c.user_id,
    agent_runs.c.started_at.desc(),
)


# Plan 20-A: durable record of every sandbox dead-transition the health
# watcher fires. The live `sandbox_crashed` SSE event only reaches UIs with
# an active chat stream open; this table backs the persistent
# "Sandbox-Abstürze" section on /settings/diagnostics so a crash that
# happens with no chat connected is still recoverable.
sandbox_crashes = Table(
    "sandbox_crashes",
    metadata,
    Column("id", Integer, primary_key=True),
    # Same workspace identifier carried by SandboxHandle/WorkspaceCrash.
    # Workspaces are configured via HERMES_WORKSPACE_ROOTS, not a DB table,
    # so this isn't an FK.
    Column("workspace_id", Text, nullable=False),
    Column("sandbox_id", Text, nullable=False),
    Column("crashed_at", Integer, nullable=False),
    # SandboxState value — 'crashed' | 'oom' | 'removed'.
    Column("state", Text, nullable=False),
    Column("exit_code", Integer),
    # Reserved for a future follow-up that pipes structured context (e.g.
    # last exec failure) through the crash handler. Always NULL today.
    Column("last_message", Text),
)

Index(
    "sandbox_crashes_crashed_at",
    sandbox_crashes.c.crashed_at.desc(),
)


# Plan 25: workspaces as a first-class managed object. The slug (`id`) is
# stable kebab-case (validated at the repository layer) and is the only
# user-controlled piece that ends up in a path — the on-disk location is
# always `${sandbox_volume_root}/${id}`, never user-controlled outside the
# slug. Soft-delete via `archived_at`.
workspaces = Table(
    "workspaces",
    metadata,
    Column("id", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
    Column("archived_at", Integer),
)


# Plan 21: always-scope tool approvals. Session-scope lives purely on
# `app.state.session_approvals` (dict[conversation_id, set[tool_name]]); only
# `allow_always` decisions need to survive a process restart. Composite PK
# (user_id, tool_name) — the same tool can be approved per-user.
tool_approvals = Table(
    "tool_approvals",
    metadata,
    Column("tool_name", Text, nullable=False),
    # Unix epoch seconds. Refreshed on re-grant (upsert) so the audit trail
    # always shows when the standing permission last became effective.
    Column("granted_at", Integer, nullable=False),
    # Reserved for a future "last used" surface; NULL until the agent loop
    # starts marking standing-allowed calls.
    Column("last_used_at", Integer),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    PrimaryKeyConstraint("user_id", "tool_name", name="tool_approvals_pk"),
)


# Plan 29-A: Personas + Channel-Prompts.
# Personas = identity ("who speaks"); channel_prompts = per-channel format
# overlay ("how the channel behaves"). The effective system prompt at run
# time is `persona.prompt + "\n\n" + channel.prompt`; channel rows may pin
# their own default persona (FK ON DELETE SET NULL) and fall back to the
# global is_default persona when NULL. The single-default invariant is
# enforced by application-level logic in the personas repository.
personas = Table(
    "personas",
    metadata,
    Column("id", Integer, primary_key=True),
    # `name` is unique PER USER (not globally) — see UniqueConstraint below.
    Column("name", Text, nullable=False),
    # Plan 36: prompt aufgesplittet in drei Fragments.
    Column("soul", Text, nullable=False, server_default=sa_text("''")),
    Column("identity", Text, nullable=False, server_default=sa_text("''")),
    Column("agents", Text, nullable=False, server_default=sa_text("''")),
    # Single-default invariant enforced at the repository layer per user.
    Column("is_default", Boolean, nullable=False, server_default=sa_text("false")),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    Column(
        "llm_credential_id",
        Integer,
        ForeignKey("llm_credentials.id", ondelete="SET NULL"),
    ),
    Column("model", Text),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    UniqueConstraint("user_id", "name", name="personas_user_name"),
)

Index("personas_user_default", personas.c.user_id, personas.c.is_default)


# Plan 36: Audit-Trail. Eine Row pro Persona-Write. `snapshot_json` enthält
# `{name, soul, identity, agents}` zum Zeitpunkt des Writes (NICHT
# `is_default` — das ist eine Sortier-Eigenschaft, keine Identität).
persona_history = Table(
    "persona_history",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "persona_id",
        Integer,
        ForeignKey("personas.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("author", Text, nullable=False, server_default=sa_text("'user'")),
    Column("snapshot_json", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
    # Denormalized from personas.user_id for RLS (Plan §1).
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
)

Index("idx_persona_history_persona", persona_history.c.persona_id)
Index("persona_history_user", persona_history.c.user_id)


# One row per channel key in `hermes.personas.CHANNEL_REGISTRY`. Seeded
# idempotently on boot. `default_persona_id` is FK ON DELETE SET NULL so
# deleting a non-default persona clears any channel that pinned it; the
# resolver then falls back to the global default persona.
channel_prompts = Table(
    "channel_prompts",
    metadata,
    Column("channel", Text, primary_key=True),
    Column("prompt", Text, nullable=False),
    Column(
        "default_persona_id",
        Integer,
        ForeignKey("personas.id", ondelete="SET NULL"),
    ),
    Column("updated_at", Integer, nullable=False),
)


# Plan 32: registered external MCP servers the agent can pull extra tools
# from. Two transports today: `http` (StreamableHTTP, `url` set) and
# `stdio` (local subprocess, `command_argv` set). `env_json` carries
# stdio environment variables as an opaque JSON map; values may be
# secrets and are NEVER returned raw — the API surface only exposes
# `env_keys`. Credentials use the same AES-GCM tripel as `llm_credentials`.
mcp_servers = Table(
    "mcp_servers",
    metadata,
    Column("id", Integer, primary_key=True),
    # Kebab-case slug, unique. Used as `mcp:<name>` source on every
    # tool the server contributes.
    Column("name", Text, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    # 'http' | 'stdio'
    Column("transport", Text, nullable=False),
    # http: full URL; stdio: NULL.
    Column("url", Text),
    # stdio: JSON-encoded argv list; http: NULL.
    Column("command_argv", Text),
    # stdio: JSON-encoded env map (may contain secrets); http: NULL.
    Column("env_json", Text),
    # AES-GCM tripel, same shape as llm_credentials.api_key_*.
    Column("credentials_iv", Text),
    Column("credentials_tag", Text),
    Column("credentials_data", Text),
    Column("enabled", Boolean, nullable=False, server_default=sa_text("true")),
    # Lifecycle manager fills this with the last start/handshake error.
    Column("last_error", Text),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)

Index("idx_mcp_servers_enabled", mcp_servers.c.enabled)


# Plan 33: reusable prompt building blocks ("skills" in the Anthropic
# sense — Markdown body with frontmatter-style metadata). Each row is a
# self-contained prompt module that personas can activate independently.
#
# Frontmatter fields (`description`, `when_to_use`) are stored as discrete
# columns rather than parsed YAML — the edit UI exposes them as separate
# inputs so neither side ever has to round-trip through YAML. `slug` is the
# stable kebab-case identifier (1..64 chars, validated at the route layer);
# `name` is the rename-friendly display label.
skills = Table(
    "skills",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    # Optional `when_to_use` frontmatter field — guidance the user writes
    # for themselves, never injected into the prompt.
    Column("when_to_use", Text, nullable=False, server_default=sa_text("''")),
    Column("body_markdown", Text, nullable=False),
    # Plan 37: disabled skills aren't added to the agent's catalog index but
    # remain editable via the UI.
    Column("enabled", Boolean, nullable=False, server_default=sa_text("true")),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)


# Plan 37 / SaaS §1: `users` table. Roles valid at this stage:
# {'platform_admin', 'member'}. `org_admin` is added in §2.
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", Text, unique=True),
    Column("role", Text, nullable=False, server_default=sa_text("'member'")),
    Column("parent_user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column(
        "bootstrap_completed",
        Boolean,
        nullable=False,
        server_default=sa_text("false"),
    ),
    Column("created_at", Integer, nullable=False),
    CheckConstraint(
        "role IN ('platform_admin','member')",
        name="users_role_valid",
    ),
)


# Per-request bearer is a SESSION token (sha256-hashed at rest). token_hash
# is UNIQUE; expires_at NULL = never.
sessions = Table(
    "sessions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("label", Text),
    Column("created_at", Integer, nullable=False),
    Column("last_used_at", Integer),
    Column("expires_at", Integer),  # NULL = never
)
Index("sessions_user", sessions.c.user_id)
