"""SQLAlchemy Core table definitions.

The non-virtual tables (conversations, messages, notes, reminders, todos)
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
    # 'signal' | 'web' | 'vscode'
    Column("channel", Text, nullable=False),
    Column("external_id", Text),
    Column("title", Text),
    # unix epoch seconds
    Column("started_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
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


reminders = Table(
    "reminders",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("due_at", Integer, nullable=False),
    Column("message", Text, nullable=False),
    Column("channel", Text, nullable=False, server_default="signal"),
    Column("fired_at", Integer),
    Column("created_at", Integer, nullable=False),
)

# Partial index ("WHERE fired_at IS NULL") — kept in schema.sql since
# SQLAlchemy 2.0 partial-index DDL is awkward across dialects.

todos = Table(
    "todos",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("content", Text, nullable=False),
    Column("tags", Text),
    Column("done_at", Integer),
    Column("created_at", Integer, nullable=False),
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
