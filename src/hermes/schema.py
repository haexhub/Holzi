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
)

metadata = MetaData()


conversations = Table(
    "conversations",
    metadata,
    Column("id", Integer, primary_key=True),
    # 'signal' | 'web' | 'vscode' | 'telegram'
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
)

Index(
    "conv_channel_updated",
    conversations.c.channel,
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
    Column("key", Text, nullable=False, unique=True),
    Column("content", Text, nullable=False),
    # comma-separated tags — YAGNI on a join table
    Column("tags", Text),
    Column("updated_at", Integer, nullable=False),
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


# Messenger inboxes. One row per (provider) account. Signal stores only
# the discovered phone number — the actual linking secret lives in the
# signal-cli container's volume, not here. Telegram stores an AES-GCM
# encrypted bot token (same hex-string pattern as llm_credentials so the
# whole DB stays text-only). The partial unique index in schema.sql
# enforces "at most one active account per provider".
# Persistent chat-run history. One row per /api/chat (and per signal /
# telegram worker turn) — the in-memory `app.state.chat_runs` cancel
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
    # 'web' | 'signal' | 'telegram' | 'vscode' — kept as Text instead of
    # an enum because the channel set is shared with conversations and
    # evolves there.
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
    # run-now). NULL for plain /api/chat or signal/telegram-driven runs. SET
    # NULL on delete: a deleted task shouldn't drag its run history with it.
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
# Plan 25-A: this table is the *only* source of truth at request time.
# The `HERMES_WORKSPACE_ROOTS` env is read exactly once at startup by the
# lifespan backfill in `main.py` (idempotent — already-seeded slugs are
# skipped); diagnostics, the workspace browser, and every git endpoint
# all read from `workspaces_repo.list_active`.
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


messenger_accounts = Table(
    "messenger_accounts",
    metadata,
    Column("id", Integer, primary_key=True),
    # 'signal' | 'telegram'
    Column("provider", Text, nullable=False),
    # 0 | 1 — at most one row per provider may be active (partial uq idx).
    Column("is_active", Integer, nullable=False, server_default="0"),
    # Signal: E.164 discovered after QR-link-as-secondary completes.
    Column("phone_number", Text),
    # Telegram-only display fields. bot_username comes from getMe after
    # the token is validated; user-facing label only — the token itself
    # lives in bot_token_*.
    Column("bot_username", Text),
    # Telegram bot token ciphertext. Hex strings, same pattern as
    # llm_credentials.api_key_*.
    Column("bot_token_iv", Text),
    Column("bot_token_tag", Text),
    Column("bot_token_data", Text),
    # Optional allowlist of chat ids the bot may respond to. JSON array
    # of stringified ints. NULL = respond to any chat the bot is in.
    Column("allowed_chat_ids", Text),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)
