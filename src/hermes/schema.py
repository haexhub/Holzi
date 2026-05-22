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
