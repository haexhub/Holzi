-- SQLite-specific schema that SQLAlchemy doesn't model directly:
-- FTS5 virtual tables, their content-sync triggers, and the partial
-- index on pending reminders. The regular tables live in `schema.py`
-- and are created via `metadata.create_all()` in `init_db()`. Statements
-- here run after the metadata create and use `CREATE ... IF NOT EXISTS`
-- so re-running on an existing DB is a no-op.

-- ---------------------------------------------------------------------------
-- messages FTS5 + sync triggers.
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  content='messages',
  content_rowid='id'
);

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
-- notes FTS5 + sync triggers.
-- ---------------------------------------------------------------------------
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
-- Partial index on pending reminders (scheduler hot-path).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS reminders_due_pending
  ON reminders(due_at) WHERE fired_at IS NULL;
