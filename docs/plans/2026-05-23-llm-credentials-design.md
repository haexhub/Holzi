# LLM Credentials in der UI — Design

> Replace the `${HOME}/.claude` bind-mount with credentials stored in `hermes.db`
> and a settings UI that the user can manage from the browser.

## Status

Draft — design agreed on 2026-05-23 (`feat/llm-credentials` branch).

## Problem

Today every LLM call goes through `haex-claude-proxy`, which expects to find
`.credentials.json` somewhere under `${PROXY_CREDENTIALS_HOME}/.claude/`. The
local-dev compose works around this by bind-mounting the operator's
`~/.claude` into the proxy container. That works for solo dev but is ugly:

- The host filesystem layout leaks into the container topology.
- A second user can't add their own credentials without a separate host.
- API-key providers (OpenAI, OpenRouter, Anthropic-direct) need a completely
  different setup — there is no UI path for them today.
- Re-running `claude login` to refresh OAuth means doing it on the host.

## Goal

A `/settings/llm` page in the Hermes web UI where the user manages credentials:

- Paste API keys for any OpenAI-compatible provider (Anthropic, OpenAI,
  OpenRouter, …)
- Run the Claude OAuth login flow from the UI itself.
- Pick which credential is "active" — that's the one Hermes uses for outgoing
  LLM calls.

Behind the scenes:

- Credentials live AES-256-GCM-encrypted in `hermes.db` (SQLite).
- `haex-claude-proxy` looks them up via a new resolver plugin that reads the
  same SQLite file from a shared Docker volume.
- The existing `HERMES_LLM_URL` / `HERMES_LLM_API_KEY` env vars stay
  supported as a fallback when no DB credential is active — sanft migration.

## Non-Goals

- Multi-tenant (Hermes stays single-user — no `owner_kind`/`owner_id`
  splitting like Specifyr).
- Per-purpose agent profiles (`llm_agent_profiles` table). One active
  credential is enough; the post-MVP `HERMES_EXTERNAL_MCP` work can revisit.
- Master-key rotation (single static key from env or auto-generated, like
  Specifyr's pattern). Document the procedure but don't automate.

## Architecture

```
┌──────────────────────┐
│  holzi-frontend      │ HTTP +
│  /settings/llm.vue   │ Bearer ──┐
└──────────────────────┘          ▼
                              ┌─────────────────────┐
                              │  hermes-server      │
                              │  /api/llm/*  (CRUD) │
                              │  /api/llm/oauth/*   │
                              │                     │
                              │  spawns:            │
                              │  claude auth login  │  ── ephemeral HOME ──┐
                              │  --claudeai         │                       │
                              └─────────────────────┘                       │
                                       │ writes                             │
                                       ▼                                    │
┌──────────────────────┐      ┌─────────────────────┐                       │
│ haex-claude-proxy    │ read │   hermes.db         │ ◄─ raw .credentials.json
│ resolver: sqlite     │ ────►│   llm_credentials   │    AES-encrypted on write
│ (new npm-plugin)     │      │   + VIEW v1         │
│ writeback on         │ write│   (shared volume)   │
│ token-refresh        │ ────►│                     │
└──────────────────────┘      └─────────────────────┘
        │
        └─► api.anthropic.com  (OAuth-Subprocess or API-key direct-forward)
```

### Components

1. **hermes-server** — Adds `llm_credentials` table, CRUD endpoints,
   OAuth-flow endpoints, and a stable read-view for the resolver.
2. **holzi-frontend** — Settings page with list, add-modal, OAuth-flow popup,
   active-credential selector.
3. **haex-claude-proxy-resolver-sqlite** — New npm package. Reads
   `hermes.db` via shared Docker volume, decrypts blobs with the same master
   key, supports `writeback()` for OAuth token refresh.
4. **docker-compose.local.yml** — Drop the `${HOME}/.claude` bind-mount;
   attach `hermes-data` volume to the proxy; share `HERMES_SECRET_KEY` with
   both containers.

## Data model

### Table `llm_credentials`

```sql
CREATE TABLE llm_credentials (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  provider        TEXT NOT NULL CHECK (provider IN ('anthropic','openai','openrouter','google','custom')),
  mode            TEXT NOT NULL CHECK (mode IN ('api_key','oauth_claude')),
  display_name    TEXT NOT NULL,
  base_url        TEXT,                 -- for custom OpenAI-compatible endpoints; NULL = provider default
  is_active       INTEGER NOT NULL DEFAULT 0,   -- 1 = the credential Hermes uses for outgoing calls

  -- api_key mode
  api_key_iv      TEXT,                 -- hex(12 bytes)
  api_key_tag     TEXT,                 -- hex(16 bytes)
  api_key_data    TEXT,                 -- hex(ciphertext)

  -- oauth_claude mode
  oauth_status    TEXT CHECK (oauth_status IN ('pending','authorized','expired')),
  oauth_authorized_at INTEGER,          -- unix epoch
  oauth_iv        TEXT,
  oauth_tag       TEXT,
  oauth_data      TEXT,                 -- hex(ciphertext of raw .credentials.json)

  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);

CREATE UNIQUE INDEX idx_llm_credentials_active
  ON llm_credentials(is_active) WHERE is_active = 1;  -- at most one active row
```

### Stable resolver view

The resolver plugin only ever reads from this view, so we can refactor the
table freely without breaking the plugin. Bumped via `_v2` etc. if the
contract ever has to change.

```sql
CREATE VIEW proxy_credentials_v1 AS
  SELECT
    id, provider, mode, base_url,
    api_key_iv, api_key_tag, api_key_data,
    oauth_iv, oauth_tag, oauth_data, oauth_status, oauth_authorized_at
  FROM llm_credentials
  WHERE is_active = 1;
```

The plugin queries `SELECT * FROM proxy_credentials_v1` and gets at most one row.

### Token writeback

When the spawned `claude` subprocess refreshes its OAuth token, the resolver's
`writeback()` callback writes the updated ciphertext back:

```sql
UPDATE llm_credentials
SET oauth_iv = ?, oauth_tag = ?, oauth_data = ?,
    oauth_authorized_at = ?, updated_at = ?
WHERE id = ?;
```

Concurrency: Hermes is the only other writer, and OAuth writes are rare
(token refresh on the order of hours). SQLite WAL mode + the default 5s
busy-timeout is enough.

## Encryption

AES-256-GCM, master key from `HERMES_SECRET_KEY` env (64 hex chars).
Auto-generate to `<data_dir>/master.key` (mode 0600) when unset — same
fallback Specifyr uses, fine for single-instance deploys.

Plaintext shapes:

- `api_key` mode: just the raw key string (e.g. `sk-ant-...`).
- `oauth_claude` mode: the raw JSON blob the `claude` CLI writes to
  `.credentials.json`. The resolver materialises it back to disk inside a
  per-request tmpfs HOME for the subprocess. Nothing persists past the call.

## API surface

All endpoints behind the existing `Authorization: Bearer ${HERMES_AUTH_TOKEN}`
gate. Single-user means no per-row permission checks needed.

```
GET    /api/llm/credentials                -- list (without ciphertext)
POST   /api/llm/credentials                -- create api_key cred
DELETE /api/llm/credentials/{id}
PATCH  /api/llm/credentials/{id}/activate  -- set is_active, clear on others

POST   /api/llm/credentials/oauth/start    -- spawn claude auth login, return {id, url}
POST   /api/llm/credentials/oauth/{id}/code -- submit the verification code
GET    /api/llm/credentials/oauth/{id}/status -- pending|authorized|expired
POST   /api/llm/credentials/oauth/{id}/cancel
```

Response shape for `GET /api/llm/credentials` (ciphertext fields omitted):

```json
[
  {
    "id": 1,
    "provider": "anthropic",
    "mode": "oauth_claude",
    "display_name": "Marko Claude Max",
    "is_active": true,
    "oauth_status": "authorized",
    "oauth_authorized_at": 1748032183,
    "created_at": 1748031000,
    "updated_at": 1748032183
  }
]
```

## OAuth flow

Lifted from Specifyr's `oauth-flow.ts` (`server/shared/utils/`):

1. `POST /api/llm/credentials/oauth/start` → row insert with
   `mode='oauth_claude'`, `oauth_status='pending'`. Spawns
   `claude auth login --claudeai` with `HOME=/tmp/hermes-oauth/<row_id>`.
   Captures the verification URL from stdout. Returns `{id, url}`.
2. UI opens the URL in a new tab. User logs in upstream, gets back a code.
3. UI POSTs the code to `POST /api/llm/credentials/oauth/{id}/code`. Backend
   writes the code to the spawned `claude` process's stdin. Process completes
   and writes `/tmp/hermes-oauth/<row_id>/.claude/.credentials.json`.
4. Backend reads that file, AES-encrypts the content, updates the row to
   `oauth_status='authorized'` + writes the ciphertext. Deletes the tmp HOME.
5. UI polls `GET /api/llm/credentials/oauth/{id}/status` until `authorized`.

Existing pending/authorized OAuth rows are cancelled and deleted before
starting a new flow (single Claude identity per Hermes instance).

## Resolver plugin contract

Per the `haex-claude-proxy` resolver interface (`src/resolvers/types.md`):

```js
// haex-claude-proxy-resolver-sqlite/src/index.js
import Database from "better-sqlite3";
import { decrypt, encrypt } from "./crypto.js";
import { writeTempHome } from "./temp-home.js";

export function create(env) {
  const dbPath = env.HERMES_DB_PATH;
  const keyHex = env.HERMES_SECRET_KEY;
  if (!dbPath || !keyHex) throw new Error("HERMES_DB_PATH + HERMES_SECRET_KEY required");
  const db = new Database(dbPath, { readonly: false });
  // WAL mode lets us read while Hermes writes
  db.pragma("journal_mode = WAL");

  return {
    name: "sqlite",
    async resolve(req) {
      const row = db.prepare("SELECT * FROM proxy_credentials_v1").get();
      if (!row) return null;            // proxy will respond with 503
      if (row.mode === "api_key") {
        const apiKey = decrypt(row, "api_key", keyHex);
        return { mode: "api_key", apiKey, baseUrl: row.base_url };
      }
      if (row.mode === "oauth_claude") {
        const plaintext = decrypt(row, "oauth", keyHex);
        const home = await writeTempHome(plaintext);
        return { mode: "subprocess", home, persistent: false, _credId: row.id };
      }
      return null;
    },
    async writeback(ctx, plaintext) {
      if (ctx.mode !== "subprocess" || !ctx._credId) return;
      const enc = encrypt(plaintext, keyHex);
      db.prepare(`
        UPDATE llm_credentials
        SET oauth_iv=?, oauth_tag=?, oauth_data=?, oauth_authorized_at=?, updated_at=?
        WHERE id=?
      `).run(enc.iv, enc.tag, enc.data, Math.floor(Date.now()/1000), Math.floor(Date.now()/1000), ctx._credId);
    },
  };
}
```

## Migration path

The env-var fallback stays in place. `hermes.config.Settings.llm_url` and
`llm_api_key` are used **only when no `is_active=1` row exists** in
`llm_credentials`. So:

- Fresh install: behaves like today (uses env vars), UI is empty.
- User creates a cred and activates it: env vars are ignored from then on.
- User deactivates all: env vars take over again.

This means the upgrade path is "open the settings page, click Add, done" —
no env-var rewrites.

## Implementation phases / PR breakdown

| # | Repo | Title | Depends on |
|---|---|---|---|
| 1 | Holzi | feat: llm_credentials schema + AES helper + CRUD endpoints | – |
| 2 | Holzi | feat: OAuth flow (subprocess + ephemeral HOME) | #1 |
| 3 | haex-claude-proxy-resolver-sqlite (new) | initial commit + tests | #1 (for view) |
| 4 | haex-claude-proxy | wire HTTP / writeback path (if needed) | – |
| 5 | holzi-frontend | feat: settings/llm UI + OAuth modal | #1, #2 |
| 6 | Holzi | feat: route LLM calls through active DB credential | #1 |
| 7 | Holzi | chore: docker-compose.local.yml — drop bind-mount, share data volume | #3, #6 |

Phases 1, 2, 6, 7 are this repo. The resolver plugin (3) and the frontend
work (5) live in their own repos.

## Open questions

- Does the proxy's PR #4 (api_key direct-forward) already accept resolver
  output of shape `{ mode: 'api_key', apiKey, baseUrl }`, or does the
  resolver contract need a small extension to carry the api_key value? Check
  in `src/server.js` of the proxy before starting phase 3.
- Token-refresh writeback: confirm the proxy's existing resolver invocation
  point supports calling `writeback()` after the spawned `claude` subprocess
  exits.
