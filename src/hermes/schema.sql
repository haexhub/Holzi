-- SQLite-specific schema that SQLAlchemy doesn't model directly:
-- FTS5 virtual tables and their content-sync triggers. The regular
-- tables live in `schema.py` and are created via `metadata.create_all()`
-- in `init_db()`. Statements here run after the metadata create and use
-- `CREATE ... IF NOT EXISTS` so re-running on an existing DB is a no-op.

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
-- llm_credentials: at most one active row + a stable read-view used by
-- the haex-claude-proxy sqlite-resolver. Bump the view to `_v2` if the
-- contract ever has to change so we can run a deprecation window without
-- breaking the plugin.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS llm_credentials_active_uq
  ON llm_credentials(is_active) WHERE is_active = 1;

CREATE VIEW IF NOT EXISTS proxy_credentials_v1 AS
  SELECT
    id, provider, mode, base_url,
    api_key_iv, api_key_tag, api_key_data,
    oauth_iv, oauth_tag, oauth_data, oauth_status, oauth_authorized_at
  FROM llm_credentials
  WHERE is_active = 1;

-- ---------------------------------------------------------------------------
-- messenger_accounts: at most one active row per provider so the
-- worker-rebuild logic in main.py can pick "the" signal/telegram account
-- without disambiguating. Drop the index to support multi-account.
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS messenger_accounts_active_per_provider
  ON messenger_accounts(provider) WHERE is_active = 1;

-- ---------------------------------------------------------------------------
-- personas (Plan 29-A): single-default invariant. Inserting or updating a
-- row with is_default=1 demotes every other row. Triggers stay simple —
-- the API layer is responsible for blocking the "demote the only default"
-- case (returns 422). FOR EACH ROW + WHEN guard keeps these no-ops for
-- inserts of is_default=0 rows.
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS personas_single_default_insert
AFTER INSERT ON personas
FOR EACH ROW WHEN NEW.is_default = 1
BEGIN
  UPDATE personas SET is_default = 0 WHERE id != NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS personas_single_default_update
AFTER UPDATE OF is_default ON personas
FOR EACH ROW WHEN NEW.is_default = 1
BEGIN
  UPDATE personas SET is_default = 0 WHERE id != NEW.id;
END;
