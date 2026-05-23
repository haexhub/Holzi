# Next session — pick up where we left off

Last session shipped (2026-05-23):

- holzi-frontend#5 — live token streaming animation (merged)
- holzi-frontend#6 — `pnpm-workspace.yaml` whitelist (merged)
- Holzi#13 — `docker-compose.local.yml` for the local dev-stack (merged)
- Holzi#14 — `llm_credentials` schema + AES-GCM helper + CRUD API
  (**open** — Phases 1 + 2 of the LLM-credentials feature)
- `haex-claude-proxy:dev` rebuilt from main after the generic-resolver
  refactor was merged. Memory updated.

Full design for the LLM-credentials feature:
[`docs/plans/2026-05-23-llm-credentials-design.md`](2026-05-23-llm-credentials-design.md).

## Where to start

1. **Confirm Holzi#14 is in a good shape** — check CodeRabbit findings,
   apply the valid ones using `feedback-coderabbit-skip-patterns` memory,
   merge.
2. Then pick up the LLM-credentials feature from **Phase 3**.

## Remaining phases

### Phase 3 — Claude OAuth flow (Holzi)

Backend endpoints to drive `claude auth login --claudeai` from the UI.
The whole shape is lifted from Specifyr's `oauth-flow.ts` but
single-user-flavoured — no `owner_kind`/`owner_id` in any of it.

New endpoints, all bearer-gated:

```
POST   /api/llm/credentials/oauth/start          spawn subprocess, return {id, url}
POST   /api/llm/credentials/oauth/{id}/code      submit verification code to stdin
GET    /api/llm/credentials/oauth/{id}/status    'pending' | 'authorized' | 'expired'
POST   /api/llm/credentials/oauth/{id}/cancel
```

Mechanics:

- `start` cancels + deletes any existing `oauth_claude` row (single Claude
  identity per Hermes instance), inserts a new `pending` row, spawns the
  subprocess with `HOME=/tmp/hermes-oauth/<row_id>`. Captures the auth URL
  from stdout. Subprocess stays alive until `code` is submitted.
- `code` writes the code to the subprocess's stdin, waits for exit, reads
  the `.credentials.json` the CLI wrote to `<HOME>/.claude/`, AES-encrypts
  it, swaps the row to `oauth_status='authorized'`, deletes the temp HOME.
- Idempotent re-runs: existing `pending` rows get torn down before a new
  flow starts (Specifyr pattern — same shape).

Tests: subprocess interactions need a fake `claude` binary in `tests/`
or `monkeypatch` of `asyncio.create_subprocess_exec`. Specifyr's
`claude-oauth-driver.ts` and its test in `server/shared/utils/__tests__/`
are the reference.

Files to create / change:

- `src/hermes/oauth.py` — subprocess driver (start, submit_code, cancel)
- `src/hermes/routes/llm.py` — add the four OAuth routes
- `tests/test_oauth_flow.py` — happy path + cancel + double-start + bad code

**Skill:** `superpowers:test-driven-development` from the start. The
subprocess paths are the kind of thing that's hard to test if you don't
build the seam first.

### Phase 4 — Agent loop reads the active DB credential (Holzi)

Today `hermes.main.build_upstream_client` reads `settings.llm_url` and
`settings.llm_api_key` at lifespan-start. That stays as fallback, but
when an `is_active=1` row exists we use that instead.

Where to plumb it:

- `src/hermes/main.py` — replace the eagerly-built `app.state.upstream`
  with a callable `get_upstream_client(db) -> AsyncClient` that the
  agent loop calls per request. Cached + invalidated on credential
  change (a `credentials_version` counter on `app.state` bumped from the
  CRUD routes is the simplest invalidation).
- `src/hermes/agent.py` — `run_agent(upstream, ...)` becomes
  `run_agent(get_upstream, ...)` (or pass `db` and resolve internally).
- `oauth_claude` mode credentials still route to `haex-claude-proxy:8080`
  — only the resolver in the proxy reads the actual creds (Phase 5).
  Hermes itself only sees the proxy URL.
- `api_key` mode credentials route to the credential's `base_url`
  (or the provider default) with `Authorization: Bearer <decrypted>`.

Tests: integration-style. Spin up an in-memory DB, insert a credential,
hit `/api/chat`, assert the outgoing request landed on the right URL with
the right Authorization header. Use the existing `_install_upstream`
test helper as a model.

### Phase 5 — Resolver plugin (`haex-claude-proxy-resolver-sqlite`, NEW REPO)

A standalone npm package that the `haex-claude-proxy` loads as a plugin
when `PROXY_RESOLVER=haex-claude-proxy-resolver-sqlite`. Plugin contract
lives in `haex-claude-proxy/src/resolvers/types.md`.

Skeleton:

```
haex-claude-proxy-resolver-sqlite/
  package.json
  src/
    index.js          create(env) -> { name, resolve, writeback }
    crypto.js         AES-256-GCM decrypt / encrypt mirroring hermes.crypto
    temp-home.js      writes plaintext into a per-request tmpfs HOME
  test/
    index.test.js     node:test, uses a real sqlite file fixture
  README.md
```

`resolve(req)` reads exactly one row from `proxy_credentials_v1` (the
stable view from Phase 1).

- `mode='api_key'` → returns `{ mode: 'api_key', apiKey, baseUrl }` for
  the proxy's direct-forward path.
- `mode='oauth_claude'` → writes the plaintext into a per-request tmp
  HOME, returns `{ mode: 'subprocess', home, persistent: false,
  _credId: row.id }`.

`writeback(ctx, plaintext)` triggers when the spawned `claude` subprocess
refreshes the OAuth token — re-encrypts and updates `oauth_iv/tag/data`
+ `oauth_authorized_at`. Uses better-sqlite3 in write mode; relies on
SQLite WAL + the default busy-timeout for the rare write-write contention
with Hermes (OAuth refreshes happen on the order of hours).

Decision needed at the start: best-sqlite3 vs node:sqlite (node 22+).
`node:sqlite` is built-in, no native dependency, but the API is newer
and less battle-tested. Probably node:sqlite for a fresh project.

Once tested, publish via `npm publish` (or just consume via a git URL
in the proxy compose, which is faster for the local-dev case).

### Phase 6 — Frontend UI (holzi-frontend)

New page `app/pages/settings/llm.vue`. Specifyr's `settings/me/llm.vue`
(122 lines) is the reference — strip the org-switcher logic.

Components:

- List of existing credentials (display_name, provider, mode, active
  badge, last-updated). Active credential is highlighted; clicking
  another credential's "activate" button POSTs to `/activate`.
- "Add API key" modal: provider dropdown (anthropic / openai /
  openrouter / google / custom), display name input, key input
  (masked), optional base_url for `custom`. POST → list refresh.
- "Add Claude OAuth" button: POST `/oauth/start` → backend returns
  `{id, url}` → open the URL in a popup window → user pastes the
  verification code into a follow-up modal → POST `/oauth/{id}/code`
  → poll `/oauth/{id}/status` until `authorized` → list refresh.
- Delete button per row (confirm modal).

Routing: add a link from the existing right-panel nav (Notes /
Todos / Reminders) or extend with a "Settings" tab. Probably a top-bar
"Settings" link is cleaner — the right panel is a work surface, not
config.

Tests: keep existing 19 Vitest tests green. Add a couple of component
tests for the OAuth modal state machine.

### Phase 7 — Compose update (Holzi)

In `docker-compose.local.yml`:

- Drop the `${HOME}/.claude` bind-mount on the proxy.
- Attach `hermes-data` volume to both `hermes-server` (already there)
  and `haex-claude-proxy`.
- Set `PROXY_RESOLVER=haex-claude-proxy-resolver-sqlite`,
  `HERMES_DB_PATH=/data/hermes.db` (the path inside the proxy
  container — it points at the same shared volume).
- Share `HERMES_SECRET_KEY` between both services. Document the
  consequence in `.env.example`.

## Order of operations for next session

Strictly: 3 → 4 → 5 → 6 → 7. Phase 5 (resolver plugin) can technically
start in parallel with Phase 4 since they don't share files, but they
both need the OAuth flow from Phase 3 to be tested end-to-end. Better
to ship them in order; finger off the parallelisation switch.

Realistic session budget: Phase 3 alone is a session. Phase 4 + 5 is
another. Phase 6 + 7 is the third. Adjust if compaction kicks in.

## Heads-up: things that might bite

- **Subprocess test isolation** (Phase 3): the `claude` CLI is a real
  binary that writes to disk. Tests must either spawn a fake binary or
  monkeypatch `asyncio.create_subprocess_exec`. Don't run the real CLI
  in CI — it'll fail without OAuth state and pollute the test runner's
  HOME.
- **DB write-write contention** (Phase 5): the proxy's resolver
  `writeback()` runs in node, Hermes runs in python. Both write to the
  same SQLite. WAL mode is already on, but bug-cause-of-the-week if
  someone forgets to enable it in the plugin.
- **`base_url` semantics** (Phase 4): when set, it overrides the
  provider default — meaning a `custom` provider with no `base_url`
  is a config error. The route validator already catches that
  (`provider='custom'` + `base_url IS NULL` should return 422), but
  the test for it isn't written yet.
- **OAuth token refresh** (Phases 4, 5): the resolver plugin's
  writeback is the single point of truth for refreshed tokens. If you
  ever change the encryption format, the plugin must understand both
  the old and the new format until the migration is done, otherwise
  the very first refresh after a deploy invalidates the credential.

## Memories to update after the feature lands

- `project_hermes_agent.md` — add "LLM credentials managed via UI; no
  more `~/.claude` bind-mount".
- `project_haex_claude_proxy.md` — add the sqlite-resolver to the
  list of known plugins.
- New memory `project_haex_claude_proxy_resolver_sqlite.md` once the
  plugin repo is live (analogous to `project_haex_claude_proxy_resolver_pg.md`).
