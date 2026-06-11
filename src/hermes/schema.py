"""SQLAlchemy Core table definitions.

The non-virtual tables (conversations, messages, notes, agent_tasks)
live here as `Table` objects. The FTS5 virtual tables and their
synchronisation triggers stay in `schema.sql` and are applied via raw SQL
in `init_db()` — SQLAlchemy doesn't natively model SQLite FTS5.
"""
from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
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
    # unix epoch seconds
    Column("started_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    # 0 | 1 — pinned threads survive the daily TTL sweep.
    Column("bookmarked", Integer, nullable=False, server_default="0"),
    # unix epoch seconds; NULL means the conversation is bookmarked
    # (never expires). Refreshed whenever `updated_at` moves.
    Column("expires_at", Integer),
    # Plan 35 §C1: owning user. server_default="1" backfills existing rows
    # on a fresh create_all (id=1 is the seeded admin). On a PRE-C1 DB the
    # column is added by the lightweight migration instead — see db.py.
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        server_default="1",
    ),
)

Index(
    "conv_channel_updated",
    conversations.c.channel,
    conversations.c.updated_at.desc(),
)

# NOTE: the per-user index `conv_user_updated` is intentionally NOT declared
# here. `conversations` is a pre-existing table, so on an existing pre-C1 DB
# `metadata.create_all` (which runs BEFORE the lightweight migration that adds
# `user_id`) would try to CREATE INDEX on a column that doesn't exist yet. The
# index is created in db.py's `_apply_lightweight_migrations` with
# `CREATE INDEX IF NOT EXISTS` after the ALTER, which is correct for both fresh
# and existing DBs. (Contrast: the brand-new `sessions` table's index lives
# here because the whole table is created fresh in one shot.)


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
)

Index("msg_conv_ts", messages.c.conversation_id, messages.c.ts)


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
)

Index("attachments_conversation", attachments.c.conversation_id)
Index("attachments_message", attachments.c.message_id)


notes = Table(
    "notes",
    metadata,
    Column("id", Integer, primary_key=True),
    # `key` is unique PER USER (not globally) — see the UniqueConstraint
    # below. The column-level `unique=True` was dropped for Wave C1.
    Column("key", Text, nullable=False),
    Column("content", Text, nullable=False),
    # comma-separated tags — YAGNI on a join table
    Column("tags", Text),
    Column("updated_at", Integer, nullable=False),
    # Plan 35 §C1: owning user. server_default="1" backfills existing rows
    # on a fresh create_all (id=1 is the seeded admin). On a PRE-C1 DB the
    # column is added by the lightweight migration instead — see db.py.
    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        server_default="1",
    ),
    # Plan 35 §C1: note keys are unique per user. This composite constraint
    # only applies to FRESH DBs (create_all). On a pre-C1 DB the old global
    # `notes.key` unique index survives — SQLite can't easily drop it, and
    # for single-user C1 a stricter global-unique key is harmless (there's
    # only user 1). C2 (real multi-user) will need a proper migration.
    UniqueConstraint("user_id", "key", name="notes_user_key"),
)

Index("notes_tags", notes.c.tags)


# Scheduled and one-shot agent runs (Plan 16). Two shapes:
#   - One-shot: `schedule = NULL`, `due_at` is the firing time. After
#     `mark_run` records the firing, `enabled` flips to 0; the row stays so
#     the user can still see its history. `due_at` is not cleared.
#   - Recurring: `schedule` is a 5-field cron expression interpreted in
#     `timezone`. `due_at` carries the *materialised* next firing — computed
#     at create time and rolled forward by `mark_run` after each tick — so
#     the scheduler's "enabled AND due_at <= now" query stays cheap and
#     index-friendly (no cron eval in the hot path).
# Invariant `due_at IS NOT NULL` enforced at the repository layer rather
# than via a DB CHECK so we can evolve the trigger shape (e.g. interval,
# ical) without an awkward SQLite table rebuild. `last_run_id` is a loose
# pointer at the agent_runs row the scheduler produced last (NULL until
# the first firing); see the column comment below for why it isn't a real FK.
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
    Column("timezone", Text, nullable=False, server_default="UTC"),
    # 0 | 1. One-shot tasks auto-flip to 0 after their single firing; the
    # pause/resume endpoint toggles this for recurring ones.
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("last_run_at", Integer),
    # 'success' | 'cancelled' | 'error' | 'running' — mirrors agent_runs.status.
    Column("last_status", Text),
    # Loose reference to agent_runs.id — no FK constraint to avoid a
    # cyclic schema with agent_runs.agent_task_id (SQLite can't add FKs
    # via ALTER, so SQLAlchemy's use_alter degrades to "no FK at all").
    # The scheduler clears this field when the underlying run row is
    # gone; a stale id is harmless (the UI falls back to "no run yet").
    Column("last_run_id", Text),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)

# Hot-path for the scheduler tick: every poll we ask "what's enabled and due?".
Index(
    "agent_tasks_enabled_due",
    agent_tasks.c.enabled,
    agent_tasks.c.due_at,
)


# AES-GCM-encrypted credentials for outgoing LLM calls. `provider` chooses
# the API flavour, `mode` whether we forward a static API key or run the
# Claude OAuth subprocess. At most one row is active at a time (enforced
# by the partial unique index in schema.sql) — that's the credential the
# agent loop picks up and the haex-claude-proxy sqlite-resolver reads.
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
    # to settings.model. Surfaced through GET /api/llm/credentials/{id}/models
    # (provider-side model listing) and persisted via PATCH .../model.
    Column("model", Text),
    # 0 | 1 — at most one row may have is_active=1 (partial unique idx).
    Column("is_active", Integer, nullable=False, server_default="0"),
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
)


# Persistent chat-run history. One row per /api/chat call (or per task
# scheduler firing) — the in-memory `app.state.chat_runs` cancel
# registry from Plan 03 becomes a thin index over rows whose status is
# still 'running'. Failure rows carry enough context (code + message +
# trace) for a "Recent failures" panel and post-hoc debugging without
# tailing container logs.
agent_runs = Table(
    "agent_runs",
    metadata,
    # run_id (uuid hex) — same id surfaced via the SSE `run` event so
    # frontend can correlate the row with the live stream.
    Column("id", Text, primary_key=True),
    # CASCADE on conversation delete: a failure row is only meaningful in
    # the context of its conversation; if the user deletes the thread the
    # history goes with it. Bookmarked conversations preserve their runs
    # automatically.
    Column(
        "conversation_id",
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # 'web' | 'task' (see hermes.personas.CHANNEL_REGISTRY) — kept as
    # Text instead of an enum because the channel set is shared with
    # conversations and evolves there.
    Column("channel", Text, nullable=False),
    Column("model", Text, nullable=False),
    # unix epoch seconds.
    Column("started_at", Integer, nullable=False),
    # NULL while the run is still in flight; filled in by the finalizer.
    Column("finished_at", Integer),
    # 'running' | 'success' | 'cancelled' | 'error'. Enforced at the
    # repository layer rather than via SQLite CHECK so we can evolve the
    # enum without an awkward table rebuild.
    Column("status", Text, nullable=False),
    # error_* columns are NULL for non-error rows. error_code mirrors the
    # codes used by routes/api.py SSE `error` events (upstream_timeout,
    # upstream_unreachable, upstream_http_error, agent_error) so the
    # frontend can map them without parsing the message.
    Column("error_code", Text),
    Column("error_message", Text),
    Column("error_trace", Text),
    # Token usage as reported by the upstream provider. NULL when the
    # provider didn't include a `usage` block (e.g. plain OpenAI stream
    # without stream_options.include_usage).
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    # Set when this run was triggered by an `agent_tasks` row (scheduled or
    # run-now). NULL for plain /api/chat-driven runs. SET NULL on delete:
    # a deleted task shouldn't drag its run history with it.
    Column(
        "agent_task_id",
        Integer,
        ForeignKey("agent_tasks.id", ondelete="SET NULL"),
    ),
)

Index(
    "agent_runs_conv_started",
    agent_runs.c.conversation_id,
    agent_runs.c.started_at.desc(),
)
Index("agent_runs_status_started", agent_runs.c.status, agent_runs.c.started_at.desc())


# Plan 20-A: durable record of every sandbox dead-transition the health
# watcher fires. Plan 11b-b's live `sandbox_crashed` SSE event only reaches
# UIs with an active chat stream open; this table backs the persistent
# "Sandbox-Abstürze" section on /settings/diagnostics so a crash that
# happens with no chat connected is still recoverable. One row per
# `(workspace_id, sandbox_id)` dead-transition — the manager's existing
# dedupe means we never re-insert for the same crashed container.
sandbox_crashes = Table(
    "sandbox_crashes",
    metadata,
    Column("id", Integer, primary_key=True),
    # Same workspace identifier carried by SandboxHandle/WorkspaceCrash.
    # Workspaces are configured via HERMES_WORKSPACE_ROOTS, not a DB table,
    # so this isn't an FK.
    Column("workspace_id", Text, nullable=False),
    # Container id of the dead sandbox — useful for cross-referencing the
    # Podman log on the host. Carried through to the API response.
    Column("sandbox_id", Text, nullable=False),
    # Unix epoch seconds when the watcher's handler fired.
    Column("crashed_at", Integer, nullable=False),
    # SandboxState value — 'crashed' | 'oom' | 'removed'. Stored as text so
    # the enum can evolve without an awkward SQLite column rebuild.
    Column("state", Text, nullable=False),
    # Container exit code when Podman exposes one; NULL for OOM / removed
    # transitions that don't carry a clean exit value.
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
# slug. `display_name` is rename-friendly UI text. Soft-delete via
# `archived_at`: rows are tombstoned, the on-disk directory stays (hard
# delete is an explicit follow-up plan).
#
# `HERMES_WORKSPACE_ROOTS` env is *bootstrap-only* (Plan 25 + 25-A): the
# lifespan backfills any slug from the env that isn't already in the
# table on boot, but no request-time code path reads the env. This table
# is the sole source of truth — the diagnostics check, the read-only
# browser, and every write/git endpoint go through
# `workspaces_repo.list_active`.
workspaces = Table(
    "workspaces",
    metadata,
    Column("id", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
    # NULL until archived; non-NULL rows are excluded from list_active by
    # default so the UI's primary view stays clean.
    Column("archived_at", Integer),
)


# Plan 21: always-scope tool approvals. Session-scope lives purely on
# `app.state.session_approvals` (dict[conversation_id, set[tool_name]]); only
# `allow_always` decisions need to survive a process restart, so only those
# get a DB row. One row per tool — granular per-argument rules are
# out-of-scope (see Plan 21's Non-Goals).
tool_approvals = Table(
    "tool_approvals",
    metadata,
    Column("tool_name", Text, primary_key=True),
    # Unix epoch seconds. Refreshed on re-grant (upsert) so the audit trail
    # always shows when the standing permission last became effective.
    Column("granted_at", Integer, nullable=False),
    # Reserved for a future "last used" surface; NULL until the agent loop
    # starts marking standing-allowed calls. Plan 21 ships the column but
    # never writes to it.
    Column("last_used_at", Integer),
)


# Plan 29-A: Personas + Channel-Prompts.
# Personas = identity ("who speaks"); channel_prompts = per-channel format
# overlay ("how the channel behaves"). The effective system prompt at run
# time is `persona.prompt + "\n\n" + channel.prompt`; channel rows may pin
# their own default persona (FK ON DELETE SET NULL) and fall back to the
# global is_default persona when NULL. The single-default invariant is
# enforced by triggers in schema.sql (`AFTER INSERT/UPDATE` flip every
# other row's is_default to 0 when a row becomes the new default).
personas = Table(
    "personas",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    # Plan 36: prompt-Blob aufgesplittet in drei Fragments. Jede
    # Spalte ist NOT NULL DEFAULT '' — Backfill (siehe Lifespan)
    # kopiert den alten `prompt` in `identity`.
    Column("soul", Text, nullable=False, server_default=""),
    Column("identity", Text, nullable=False, server_default=""),
    Column("agents", Text, nullable=False, server_default=""),
    # 0 | 1 — single-default trigger keeps at most one row with 1.
    Column("is_default", Integer, nullable=False, server_default="0"),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
    Column(
        "llm_credential_id",
        Integer,
        ForeignKey("llm_credentials.id", ondelete="SET NULL"),
    ),
    Column("model", Text),
)


# Plan 36: Audit-Trail. Eine Row pro Persona-Write. `snapshot_json`
# enthält `{name, soul, identity, agents}` zum Zeitpunkt des Writes
# (NICHT `is_default` — das ist eine Sortier-Eigenschaft, keine
# Identität). `author` ist heute fix `'user'`; Wave C ersetzt mit
# echter user_id.
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
    Column("author", Text, nullable=False, server_default="user"),
    Column("snapshot_json", Text, nullable=False),
    Column("created_at", Integer, nullable=False),
)

Index("idx_persona_history_persona", persona_history.c.persona_id)


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
# `env_keys`. Credentials (e.g. a bearer token for HTTP transport) use
# the same AES-GCM tripel as `llm_credentials`. `last_error` mirrors the
# Plan 11b crashed-sandbox pattern: the manager fills it on a failed
# start/handshake and clears it on a successful restart.
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
    # The API redacts this to `env_keys` on the way out.
    Column("env_json", Text),
    # AES-GCM tripel, same shape as llm_credentials.api_key_*.
    Column("credentials_iv", Text),
    Column("credentials_tag", Text),
    Column("credentials_data", Text),
    Column("enabled", Integer, nullable=False, server_default="1"),
    # Lifecycle manager fills this with the last start/handshake error.
    Column("last_error", Text),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)

Index("idx_mcp_servers_enabled", mcp_servers.c.enabled)


# Plan 33: reusable prompt building blocks ("skills" in the Anthropic
# sense — Markdown body with frontmatter-style metadata). Each row is a
# self-contained prompt module that personas can activate independently.
# The resolver in `hermes.personas` mixes the active bodies into the
# composed system prompt between persona and channel.
#
# Frontmatter fields (`description`, `when_to_use`) are stored as
# discrete columns rather than parsed YAML — the edit UI exposes them as
# separate inputs so neither side ever has to round-trip through YAML.
# `slug` is the stable kebab-case identifier (1..64 chars, validated at
# the route layer); `name` is the rename-friendly display label.
skills = Table(
    "skills",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("slug", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    # Optional `when_to_use` frontmatter field — guidance the user
    # writes for themselves, never injected into the prompt.
    Column("when_to_use", Text, nullable=False, server_default=""),
    Column("body_markdown", Text, nullable=False),
    # Plan 37: NICHT in den Catalog-Index aufgenommen wenn enabled=0.
    # Body bleibt erreichbar via `skill_load(name)` — disabled Skills
    # sind dem Agent *unsichtbar*, aber im UI weiterhin editierbar.
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)


# Plan 37: Minimal-`users`-Tabelle, Wave-C-vorbereitend (Plan 35
# §C1). Single-User-Box bis Wave C — eine Seed-Row mit id=1. Wave C
# erweitert per ALTER TABLE ADD COLUMN um email / password_hash /
# role / parent_user_id (kein zweiter Drop-and-Recreate).
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("email", Text, unique=True),
    Column("role", Text, nullable=False, server_default="member"),
    Column("parent_user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column(
        "bootstrap_completed",
        Integer,
        nullable=False,
        server_default="0",
    ),
    Column("created_at", Integer, nullable=False),
)


# Plan 35 §C1: per-request bearer is a SESSION token (sha256-hashed at rest).
# token_hash is UNIQUE; expires_at NULL = never. New table → created (with its
# index below) by metadata.create_all on both fresh and existing DBs.
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
