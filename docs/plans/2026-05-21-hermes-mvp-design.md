# Hermes Personal Agent — MVP Design

**Status:** Brainstorm finalised 2026-05-21. Implementation in dedicated sessions per phase.

**Goal:** A personal AI assistant ("Hermes") running on a VPS, reachable via Signal (Note-to-Self), a web UI, and from inside VSCode through Cline/Roo Code. Uses your Claude Max subscription via the `haex-claude-proxy`. Maintains memory across all channels.

---

## 1. Component Layout

```
┌──────────────── VPS (Hetzner / Netcup) ────────────────────┐
│                                                            │
│  Traefik (:443) ───── Let's Encrypt HTTPS                  │
│      │ EXTERNAL by default — most boxes have one already.  │
│      │ Compose ships it under `--profile traefik` for      │
│      │ standalone installs. Either way, service containers │
│      │ carry the Traefik labels and the running Traefik    │
│      │ discovers them via the Docker provider.             │
│      ▼                                                     │
│  hermes-server (Python, FastAPI, :8082)                    │
│      │                                                     │
│      ├─ Bearer-Token middleware (auth in app, not ingress) │
│      ├─ Memory: SQLite + FTS5 (/data/hermes.db)            │
│      ├─ Signal Worker → polls signal-cli-rest-api          │
│      ├─ MCP SERVER (HTTP/SSE) over /mcp                    │
│      │     → exposes Hermes-own tools (recall_memory etc.) │
│      ├─ MCP CLIENT (HTTP)                                  │
│      │     → connects out to configured external MCP       │
│      │       servers (haex-vault, future plugins);         │
│      │       merges their tools into the agent loop        │
│      ├─ LLM-proxy layer (OpenAI-compatible) over /v1       │
│      ├─ Web UI backend (REST/SSE) over /api                │
│      └─ Reminder scheduler (in-process cron loop)          │
│                                                            │
│  signal-cli-rest-api (Docker, internal :8081)              │
│      │ linked device to your Signal account                │
│      │ persistent volume for state                         │
│                                                            │
│  haex-claude-proxy (Docker, internal :8080)                │
│      │ PROXY_RESOLVER=file, OAuth from claude login        │
│      │ persistent volume for ~/.claude                     │
│                                                            │
│  Optional: hermes-frontend (Nuxt 3) served statically by   │
│  hermes-server from /opt/hermes/frontend/dist              │
│                                                            │
└────────────────────────┬───────────────────────────────────┘
                         │ HTTPS, Bearer-Token
       ┌─────────────────┼──────────────────┬────────────────┐
       ▼                 ▼                  ▼                ▼
   Desktop           Smartphone         Anywhere       haex-vault
   - VSCode + Cline  - Web browser      - Web browser   (Nuxt app,
     (LLM URL =        (Hermes Web UI                    Haextension
      Hermes /v1)       OR HaexChat                      HaexChat →
                        Haextension)                     Hermes /v1)
                                                              ▲
                                                              │
                                                  Hermes MCP-client
                                                  connects here too
                                                  → vault.* tools
```

**Two Signal/MCP ingress notes:**
- **Signal worker** polls `signal-cli-rest-api` over the internal Docker network. It doesn't go through Traefik. Bearer-Token protects only the HTTPS endpoints.
- **MCP-client side**: Hermes initiates outbound HTTPS to configured external MCP servers (haex-vault and friends), with their own auth tokens, configured via env or a small JSON/YAML.

---

## 2. Component Inventory

### 2.1 `haex-claude-proxy` (already exists)
- Reused as-is from the new generic-resolver branch
- `PROXY_RESOLVER=file PROXY_CREDENTIALS_HOME=/data/claude` (mounted volume with a one-time `claude login` performed via an interactive `docker run --rm -it`)
- No DB, no plugin, no auth — locked behind Docker network only

### 2.2 `signal-cli-rest-api` (bbernhard/signal-cli-rest-api)
- Docker image, `MODE=json-rpc`
- Linked device — initial setup uses `qrencodeshow` once interactively
- Persistent volume `/home/.local/share/signal-cli` for keys/state
- Exposes `/v1/receive/<number>` (long-poll) and `/v2/send`

### 2.3 `hermes-server` (Python, new)
- **Framework:** FastAPI + Uvicorn
- **LLM client:** Anthropic Python SDK pointed at `haex-claude-proxy/v1`
- **DB:** SQLite via `aiosqlite` (async)
- **Agent loop:** Claude Agent SDK in Python (or hand-rolled tool-use loop — TBD per phase)
- **MCP server:** the `mcp` Python SDK, served over HTTP/SSE
- **Reminders:** in-process asyncio loop, checks DB every minute
- **Web UI:** static files served from disk (`/opt/hermes/frontend/dist`)

### 2.4 `holzi-frontend` (Nuxt 4 SPA, **separate repo**)
Lives at https://github.com/haexhub/holzi-frontend (Phase 9, started 2026-05-22). Decision-rationale for the split: Python+uv vs. Node+pnpm tooling don't co-exist cleanly in one PR-pipeline, build artefact (`dist/`) is what crosses the boundary anyway, and FastAPI's `/openapi.json` is enough to keep API types in sync without source-coupling.

- **Nuxt 4** (Vue 3 + Composition API, TypeScript)
- SSR turned off; built as static SPA (Hermes-server serves `dist/`)
- **Tailwind 4** + **shadcn-vue** primitives (`app/components/ui/`)
- **Pinia** for store, **VueUse** for composables (incl. `useLocalStorage`)
- **pnpm** package manager
- Chat view with SSE consumption from `/api/chat` (parses `session`/`text`/`done`/`error` events)
- Sidebar: conversation list (filtered by `channel=web`)
- Right panel: Notes / Todos / Reminders tabs (one-call-per-endpoint CRUD)
- Auth: Bearer-token entered once on `/login`, persisted in localStorage; 401 clears token and bounces back to login

### 2.5 `traefik` (Docker, optional)
- External-by-default: most target machines already run a Traefik. Service containers in this Compose carry the standard `traefik.enable=true` + `traefik.http.routers.*` labels so any host-running Traefik picks them up automatically via the Docker provider.
- For greenfield boxes: `docker compose --profile traefik up` brings up a bundled Traefik with Let's Encrypt + ACME-HTTP-01 challenge against the configured `HERMES_DOMAIN`.
- Auth lives in **FastAPI**, not in the proxy. That way the same `Bearer` check applies whether requests come through your own Traefik, the bundled one, or a localhost dev setup with no proxy at all.

**Network-access principle (explicit non-decision: no VPN).** Hermes is publicly reachable over HTTPS — no Tailscale, no WireGuard, no mesh-VPN. Reasoning: every client device (Signal app on phone, haex-vault, VSCode, browser anywhere) must work without first installing a VPN client. The trade-off is that the public Hermes endpoint sits on the open internet with only `Bearer`-token auth between it and an attacker, so:

- `HERMES_AUTH_TOKEN` is a 32-byte (64-hex) random secret generated once on the server (`openssl rand -hex 32`). Treat it like an SSH key — never check into git, never paste into chat.
- Traefik picks up the standard `ratelimit` middleware in front of `/v1/*` and `/api/chat*` (e.g. 10 req/min/IP for failed auth). Configurable; defaults documented in the deploy chapter.
- Optional: fail2ban watching the Hermes app log for `401 Unauthorized` and blocking offending IPs at the host firewall. Out-of-scope for MVP, called out as a hardening follow-up.
- HTTPS is mandatory in production (Let's Encrypt via Traefik). HTTP-only is only OK for localhost dev.

### 2.6 External MCP servers (post-MVP integration point)
- `hermes-server` reads an env-var or small config (`HERMES_EXTERNAL_MCP`) listing `{ name, url, token? }` entries.
- On startup: open MCP-client connections, fetch each server's manifest, and merge its tools into the agent's tool catalogue. Tools are namespaced (`vault.create_sync_rule`, etc.) to avoid collisions.
- First concrete target: [[project-haex-vault]] — gives Hermes the ability to manage sync rules, search files, list spaces, etc. on behalf of the user. See section 6 for details.

---

## 3. Data Model (SQLite)

```sql
-- Conversations are per-channel threads.
CREATE TABLE conversations (
  id          INTEGER PRIMARY KEY,
  channel     TEXT NOT NULL,        -- 'signal' | 'web' | 'vscode'
  external_id TEXT,                 -- optional: VSCode workspace id, Signal thread id
  title       TEXT,
  started_at  INTEGER NOT NULL,     -- unix epoch
  updated_at  INTEGER NOT NULL
);
CREATE INDEX conv_channel_updated ON conversations(channel, updated_at DESC);

CREATE TABLE messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  role            TEXT NOT NULL,    -- 'user' | 'assistant' | 'tool'
  content         TEXT NOT NULL,    -- plain text; tool-use blocks serialised as JSON
  ts              INTEGER NOT NULL,
  meta_json       TEXT              -- optional: tool name, model, tokens used
);
CREATE INDEX msg_conv_ts ON messages(conversation_id, ts);

CREATE VIRTUAL TABLE messages_fts USING fts5(
  content,
  content='messages',
  content_rowid='id'
);
-- Triggers keep FTS in sync (insert/update/delete on messages → FTS).

-- Notes are persistent facts unattached to any conversation.
CREATE TABLE notes (
  id         INTEGER PRIMARY KEY,
  key        TEXT NOT NULL UNIQUE,  -- 'project.holzi.status', 'user.preferences.coding'
  content    TEXT NOT NULL,
  tags       TEXT,                  -- comma-separated for now (YAGNI on a join table)
  updated_at INTEGER NOT NULL
);
CREATE INDEX notes_tags ON notes(tags);

CREATE VIRTUAL TABLE notes_fts USING fts5(
  key, content, tags,
  content='notes',
  content_rowid='id'
);

CREATE TABLE reminders (
  id          INTEGER PRIMARY KEY,
  due_at      INTEGER NOT NULL,    -- unix epoch
  message     TEXT NOT NULL,
  channel     TEXT NOT NULL,       -- 'signal' default
  fired_at    INTEGER,             -- null until fired
  created_at  INTEGER NOT NULL
);
CREATE INDEX reminders_due ON reminders(due_at) WHERE fired_at IS NULL;

CREATE TABLE todos (
  id         INTEGER PRIMARY KEY,
  content    TEXT NOT NULL,
  tags       TEXT,
  done_at    INTEGER,              -- null = open
  created_at INTEGER NOT NULL
);
```

---

## 4. API Surfaces

### 4.1 `POST /v1/chat/completions` — OpenAI-compatible LLM endpoint

This is the surface Cline/Roo Code talks to. Internally:

1. Validate Bearer token.
2. Extract or assign a `conversation_id` from headers (`X-Hermes-Session`) or the system message of the first request.
3. Load conversation history + relevant notes (FTS5 against the user's last message).
4. Build the prompt: system prompt (Hermes personality) + memory context + the inbound messages.
5. Forward to `haex-claude-proxy/v1/chat/completions`.
6. Stream the response back to the caller (SSE pass-through).
7. After the response is complete: persist user-and-assistant messages to `messages`, update conversation.

### 4.2 `POST /mcp` (SSE) — MCP server

Tools exposed (12 total):

**Memory:**
- `recall_memory(query: str, channel: str?, limit: int = 10)` → FTS5 over `messages` and `notes`, ranked
- `list_conversations(channel: str?, since_unix: int?, limit: int = 20)` → list of `{id, title, channel, updated_at, message_count}`
- `get_conversation(id: int, limit: int = 50)` → ordered messages
- `save_note(key: str, content: str, tags: list[str] = [])` → upsert
- `get_note(key: str)` / `find_notes(query: str, tags: list[str] = [])` → search

**Cross-channel:**
- `cross_channel_send(channel: str, message: str)` → send via Signal worker (others later)

**External info:**
- `web_search(query: str, n: int = 5)` → Brave Search API (cheap, no auth dance) or Tavily
- `url_fetch(url: str)` → fetch + extract main content (`trafilatura` or `readability-lxml`)

**Productivity:**
- `reminder_set(when: str, message: str, channel: str = "signal")` → parse natural-language time via `dateparser`, schedule
- `reminder_list(include_fired: bool = false)`
- `todo_add(content: str, tags: list[str] = [])`
- `todo_list(filter: str?)` → tag/done filter
- `todo_done(id: int)`

### 4.3 `GET /mcp/manifest` — MCP discovery

Returns the tool catalogue in MCP JSON-schema format. Cline/Roo Code reads this to know what's available.

### 4.4 `/api/*` — Web UI backend

REST + SSE endpoints consumed by the Nuxt frontend. Sketch:
- `GET /api/conversations?channel=...`
- `GET /api/conversations/{id}/messages`
- `POST /api/chat` (returns SSE stream of new assistant message)
- `GET /api/notes`, `POST /api/notes`, `PUT /api/notes/{key}`, `DELETE /api/notes/{key}`
- `GET /api/todos`, `POST /api/todos`, `PUT /api/todos/{id}`, `DELETE /api/todos/{id}`
- `GET /api/reminders`, `POST /api/reminders`
- `GET /api/health`

### 4.5 Signal-Worker (no HTTP surface)

In-process async task started on app boot. Loop:
1. Long-poll `signal-cli-rest-api:/v1/receive/<number>` with a 30s timeout.
2. For each incoming envelope: skip unless `source == destination == self_number` (Note-to-Self filter).
3. Identify-or-create a `conversation` for channel `'signal'`.
4. Persist the user message.
5. Run the agent loop (same internal entry point as the chat endpoint) with the conversation history + Hermes' own MCP tools.
6. Stream-or-buffer the assistant reply, then `POST /v2/send` to Signal.

---

## 5. Phases (high-level)

Each phase ends with: working tests, a commit, and a manual smoke. Detail-plans get written per phase via the `writing-plans` skill when implementation starts.

### Phase 0 — Project skeleton
- `git init`, Python 3.12+ `pyproject.toml` (poetry or uv), Docker Compose with placeholder for each container, `.env.example`, `Makefile` or `justfile`.
- One-pager `README.md`.

### Phase 1 — Hermes-server skeleton
- FastAPI app, `/healthz`, Bearer-token middleware, structured logging.
- Container builds, Compose brings it up alongside the proxy + signal-cli-rest-api.

### Phase 2 — Memory layer
- SQLite schema migrations (Alembic or hand-rolled — pick one).
- `aiosqlite` connection pool, repository helpers for conversations/messages/notes.
- FTS5 triggers verified.
- Unit tests for query functions (in-memory SQLite).

### Phase 3 — LLM-proxy layer
- `POST /v1/chat/completions` endpoint (OpenAI-compatible).
- Forwards to `haex-claude-proxy`, streams response.
- Conversation persistence around the call.
- Tests: a fake upstream proxy that returns canned responses.

### Phase 4 — Signal worker
- One-time interactive linking against `signal-cli-rest-api`.
- Long-poll loop, Note-to-Self filter, message persistence.
- Reply hard-coded to "received" for now (agent comes in phase 5).
- Manual smoke: send a Note-to-Self, observe persistence + canned reply.

### Phase 5 — Agent loop (used by both Signal worker and `/api/chat`)
- Tool-use loop driving `haex-claude-proxy` and Hermes' own MCP server.
- System prompt + memory injection.
- Streaming back to caller.
- Tests: tool-use round-trip against the canned-proxy from phase 3.

### Phase 6 — MCP server
- `POST /mcp` SSE endpoint, `GET /mcp/manifest`.
- Tools: `recall_memory`, `list_conversations`, `get_conversation`, `save_note`, `get_note`, `find_notes`, `cross_channel_send`.
- Tests: per-tool round-trip via the `mcp` SDK.

### Phase 7 — External + productivity tools
- `web_search` (Brave API; env-var key) — fallback to Tavily.
- `url_fetch` (trafilatura).
- `reminder_*` + `todo_*` (DB + scheduler).
- Reminder fires through the Signal worker's send path.

### Phase 8 — Web UI backend
- REST + SSE endpoints under `/api/*`.
- Reuse the same agent loop as Signal/Cline.

### Phase 9 — Web UI frontend
- Nuxt 3 project, built statically, served by hermes-server from disk.
- Chat view, conversation sidebar, notes/todos panels.
- Bearer-token form on first load.

### Phase 10 — Production deploy
- Docker-Compose profile `traefik` for boxes without an existing Traefik (bundles the proxy + ACME).
- Service-container labels for external Traefik discovery (default path).
- Docker Compose finalised (volumes, networks, restart policies).
- Systemd timer for daily SQLite backup to S3 / B2 / local rsync.

### Post-MVP — External MCP integration
- `HERMES_EXTERNAL_MCP` config (JSON list of `{ name, url, token? }`).
- MCP-client manager: open connections at boot, fetch manifests, merge tools into agent loop with `<name>.<tool>` namespace.
- First concrete client: [[project-haex-vault]]. See section 6.6.

---

## 6. Risks / open questions

1. **Conversation continuity across channels.** A "session" in Cline (one VSCode workspace) maps cleanly to one `conversation`. A "session" on Signal is fuzzier — every Note-to-Self message could be the same conversation or a new one. Likely heuristic: same conversation if previous message < 6h ago, else new. Settle in phase 4.

2. **Tool-use orchestration.** Cline's LLM call already comes with tool definitions (Cline's own filesystem/terminal tools). Hermes' LLM-proxy needs to *merge* Hermes-MCP-tools into that tool-use loop. Two designs are possible: (a) Hermes injects its tools as additional `tools[]` entries in the upstream request — Cline's agent can call them directly; (b) Hermes exposes them only via the MCP endpoint, Cline subscribes separately. (a) is simpler. Decide in phase 5.

3. **OAuth-token refresh under load.** The proxy refreshes Claude OAuth tokens inside the spawned `claude` subprocess. If two requests arrive simultaneously, both spawns try to refresh against the same `~/.claude/.credentials.json` and one will overwrite the other. For Hermes (single user, low concurrent traffic) this is unlikely to bite, but the proxy's FileResolver has no in-process lock. Note for ops.

4. **Bearer-token as only line of defense.** Hermes is publicly exposed (no VPN by design). Mitigations baked into §2.5: long random token, Traefik rate-limit middleware on auth-sensitive routes, HTTPS mandatory. Hardening follow-ups (fail2ban, token rotation, short-lived JWT + refresh, IP allowlist for known devices) are deferred to a security-pass after MVP. A determined attacker on a client device could also exfiltrate the token from browser localStorage — that's a separate concern, accepted for now because all clients are personal devices.

5. **Signal-bot ToS.** Linked devices are a normal Signal feature; using one as a bot endpoint is not officially forbidden but is a grey area. Risk is rate-limiting or future API tightening, not an account ban (verified through community projects).

---

## 6.6 haex-vault integration (post-MVP, but architected for from day one)

[[project-haex-vault]] is the user's existing local-first / E2E-encrypted data + file sync runtime (CRDT, MLS, Haextensions). Two integration vectors:

**a) HaexChat (frontend role).** A Haextension inside haex-vault talks to Hermes' `/v1/chat/completions` like any other client. Carries the Bearer token, gets streaming SSE responses. From Hermes' side: indistinguishable from Cline or the bundled Nuxt UI. No special server-side work.

**b) Vault-as-MCP-server (agent capability).** haex-vault exposes an MCP server speaking the standard MCP HTTP/SSE protocol. Tools (illustrative — actual catalog defined by haex-vault):
- `vault.list_spaces()`
- `vault.find_file(query, space?)`
- `vault.create_sync_rule(source, target, schedule)`
- `vault.list_sync_rules()`
- `vault.read_file(path)` (E2E-decrypted server-side; transports plaintext to Hermes only)

Hermes connects as an MCP client (configured via `HERMES_EXTERNAL_MCP`). When the user says "sync my phone pictures to S3", the agent's tool-use loop reaches `vault.create_sync_rule` and the rule is created — no human round-trip needed.

**Decisions deferred to that integration:**
- Trust model: does Hermes get a long-lived vault token, or short-lived per-session? Probably long-lived for now (single-user, same trust boundary as Hermes itself).
- Network topology: vault on same VPS (internal Docker network) or remote box reachable via public HTTPS + Bearer.

---

## 6.7 Implementation status (snapshot 2026-05-22)

| Phase | PR | Status |
|---|---|---|
| 0 — project skeleton | — (root commits) | ✅ on main |
| 1 — hermes-server skeleton | — (root commits) | ✅ on main |
| 2 — SQLite memory layer | #1 | ✅ on main |
| 3 — `/v1/chat/completions` proxy | #2 | ✅ on main |
| 4 — Signal worker | #3 | ✅ on main |
| 5 — agent loop (+ LLM provider flexibility) | #4 | ✅ on main |
| 6 — MCP server + 7 tools | #5 | ✅ on main |
| 7 — productivity/external tools + scheduler | #6 | ✅ on main |
| 8 — web-UI backend (`/api/*`, recursion guard) | #7 | ⏳ open on `phase-8-web-ui-backend` |
| 9 — Nuxt frontend (separate repo: holzi-frontend) | — | ⏳ initial scaffolding pushed to https://github.com/haexhub/holzi-frontend |
| 10 — production deploy | — | ⏭ |

**Important deviation from the original plan.** Phase 5 also delivered the *LLM-provider-flexibility* change: `HERMES_PROXY_URL` became `HERMES_LLM_URL` and `HERMES_LLM_API_KEY` was added so the upstream can be any OpenAI-compatible endpoint, not only `haex-claude-proxy`. The renaming is reflected in `.env.example`, `docker-compose.yml`, and the README provider table.

Phase 6 added a deliberate scope decision: the Signal worker still calls `run_agent` with `tools=None`, even though the tool catalogue now exists. Phase 8 is the first internal caller that hands the catalogue to the agent loop — via the new `/api/chat` endpoint. The catalog is built per request with `current_channel="web"` (see `hermes.tool_catalog.build_tool_catalog`); `cross_channel_send` refuses to write back to the channel that produced the request (recursion guard). The Signal worker is intentionally **not** switched over in this phase and stays at `tools=None` until we revisit it.

**Phase 8 surfaces shipped.**
- `POST /api/chat` — JSON in (`message`, optional `conversation_id`), SSE out with `session` / `text` / `done` events. Runs the full agent loop with the Hermes tool catalog scoped to `current_channel="web"`.
- `GET /api/conversations`, `GET /api/conversations/{id}` — list + detail with messages.
- `GET/POST/PUT/DELETE /api/notes` (+ `GET /api/notes/{key}`) — REST CRUD over the notes repo.
- `GET/POST/PATCH/DELETE /api/todos` — REST CRUD; PATCH only supports `{"done": true}` (mark as completed).
- `GET/POST/DELETE /api/reminders` — REST CRUD.

**Repo additions (minimal, idiomatic to the existing hand-SQL style):** `notes.list_all`, `notes.delete`, `todos.delete`, `reminders.delete`. A follow-up PR will migrate the entire repository layer to SQLAlchemy Core (async) — see "Repo-layer refactor (queued)" below.

**Streaming follow-up (post-Phase-8).** `run_agent` gained an optional `on_chunk` callback in a follow-up branch: when set, the upstream call uses `stream=True` and every `delta.content` is forwarded incrementally. Tool-call deltas are still assembled across chunks (id/type/name first, function.arguments concatenated). `/api/chat` now bridges this via an `asyncio.Queue` and emits one SSE `text` event per upstream chunk — the frontend's `useChatStream` already concatenates multiple `text` events, so no client change was needed. The Signal worker / MCP keep using the non-streaming JSON path (`on_chunk=None`, full backward compat).

## 6.8 Repo-layer refactor (queued)

After Phase 8 merges, the entire `hermes.repository` layer will move from hand-rolled `aiosqlite` SQL to **SQLAlchemy Core async** in a separate PR. Reason: Marko prefers a typed query builder (Drizzle-style) over plain SQL text. SQLAlchemy Core stays close to raw SQL (no ORM magic), supports SQLite + FTS5 (via `text()` / custom constructs for FTS), and is the most established async-capable option in Python. The migration is intentionally scope-isolated from Phase 8 to keep that PR a pure feature change.

## 7. Out of scope (defer until after MVP works)

- Multi-user support (just you for now)
- Mobile-native app (web UI on phone is fine)
- Voice messages
- Cross-account migration tooling
- Vector/RAG retrieval (FTS5 is enough until conversations > 10k)
- Encryption-at-rest on `hermes.db` (the VPS disk encryption + TLS via Traefik is the trust boundary for now)
- VSCode Hermes-side extension (Cline/Roo Code as client is enough)
- haex-vault HaexChat extension + Vault-MCP-tools (designed for in §6.6, built later)
