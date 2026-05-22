-- Hermes schema. Idempotent — `CREATE ... IF NOT EXISTS` everywhere so
-- init_db() can run at every boot without migrations until we introduce
-- versioning. Schema changes that aren't pure additions need a real
-- migration story (added in a later phase).

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Conversations: per-channel threads.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
  id          INTEGER PRIMARY KEY,
  channel     TEXT NOT NULL,         -- 'signal' | 'web' | 'vscode'
  external_id TEXT,                  -- VSCode workspace id, Signal thread id, ...
  title       TEXT,
  started_at  INTEGER NOT NULL,      -- unix epoch seconds
  updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS conv_channel_updated
  ON conversations(channel, updated_at DESC);

-- ---------------------------------------------------------------------------
-- Messages.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,     -- 'user' | 'assistant' | 'tool'
  content         TEXT NOT NULL,     -- plain text; tool-use blocks serialised as JSON
  ts              INTEGER NOT NULL,
  meta_json       TEXT               -- optional: tool name, model, tokens used
);

CREATE INDEX IF NOT EXISTS msg_conv_ts ON messages(conversation_id, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  content='messages',
  content_rowid='id'
);

-- Keep FTS in sync with the content table.
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

-- ---------------------------------------------------------------------------
-- Notes: persistent facts unattached to any conversation.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY,
  key        TEXT NOT NULL UNIQUE,
  content    TEXT NOT NULL,
  tags       TEXT,                   -- comma-separated; YAGNI on a join table
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS notes_tags ON notes(tags);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  key, content, tags,
  content='notes',
  content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, key, content, tags)
    VALUES (new.id, new.key, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, key, content, tags)
    VALUES ('delete', old.id, old.key, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, key, content, tags)
    VALUES ('delete', old.id, old.key, old.content, old.tags);
  INSERT INTO notes_fts(rowid, key, content, tags)
    VALUES (new.id, new.key, new.content, new.tags);
END;

-- ---------------------------------------------------------------------------
-- Reminders (Phase 7).
-- Scheduled outbound messages. The scheduler loop polls the table once per
-- minute and fires every row whose due_at <= now() and fired_at IS NULL.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
  id         INTEGER PRIMARY KEY,
  due_at     INTEGER NOT NULL,        -- unix epoch
  message    TEXT NOT NULL,
  channel    TEXT NOT NULL DEFAULT 'signal',
  fired_at   INTEGER,                 -- null until the scheduler has delivered it
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS reminders_due_pending
  ON reminders(due_at) WHERE fired_at IS NULL;

-- ---------------------------------------------------------------------------
-- Todos (Phase 7).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS todos (
  id         INTEGER PRIMARY KEY,
  content    TEXT NOT NULL,
  tags       TEXT,                    -- comma-separated
  done_at    INTEGER,                 -- null = open
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS todos_open ON todos(done_at) WHERE done_at IS NULL;
