> **SUPERSEDED** by [`2026-06-11-saas-coding-agent-design.md`](2026-06-11-saas-coding-agent-design.md) §1 — SQLite framing replaced by Postgres + RLS. The structural decisions in this doc still apply.

# Wave C2 — Email Magic-Link Login, Invites, Roles & Sharing (OUTLINE)

> **For Claude:** OUTLINE, not a task-by-task plan. Firm this up into a full
> TDD plan **after C1 has landed** — decisions here depend on C1's final
> shapes (the `IdentityResolver`, the `sessions` table, how `user_id` is
> threaded, the per-user default-persona seeding). Re-read the C1 design doc
> + Plan 35 §C2 before expanding.
>
> Depends on: **C1 — SHIPPED 2026-06-11** (`2026-06-11-wave-c1-multi-user-impl.md`).
> Blocks: **C3** (per-user runtime isolation).
> New in C2 from the C1 review: **B8** below collects the multi-user concerns
> C1 deliberately deferred (agent tool-loop identity, per-user channel
> prompts, per-user bootstrap flag) — these become live the moment a second
> user exists, so they are required C2 work, not optional.

**Goal:** Everyone logs in via **email magic-link → short-lived session**
(no passwords, no long-lived token in `localStorage`). A family admin invites
partner / kids / parents by email; each gets their own
conversations/notes/personas, a role (`admin` / `member` / `child`), and the
admin can share infra resources (LLM credential, skills, MCP servers) with
them. A `child` is forced through tool-approval on every call.

**Decisions carried from the 2026-06-11 design discussion:**
- Per-request bearer is a **session token** (C1's `sessions` table).
- Login = **email magic-link, for everyone** — adults, children (admin
  provides their email), and the VS Code frontend.
- Sessions are **ephemeral by default** (sessionStorage / short TTL); "stay
  signed in" is opt-in on trusted devices (localStorage / long TTL).
- **SMTP is an accepted new dependency.**

## Why this is the bulk of the user-facing work

C1 made the backend *capable* of many users and made the bearer a session,
but created no users and no way to mint a session except the bootstrap admin
token. C2 is where a second human logs in by email, where roles gain
meaning, and where the "child has no API key" problem is solved (sharing).

## Building blocks (each becomes 1-3 plan tasks when expanded)

### B1 — Email transport + one-time `login_tokens`

- **SMTP config** in `config.py`: `HERMES_SMTP_HOST/PORT/USER/PASSWORD/FROM`
  (+ a no-op/console transport for dev & tests so the suite needs no real
  SMTP). A thin `mailer.py` with `send_login_link(email, url, code)`.
- **`login_tokens` table**: `(id, user_id, token_hash UNIQUE, purpose,
  created_at, expires_at, consumed_at)`. `purpose ∈ {login, invite}` — the
  same one-time-token machinery backs both magic-link login and invites.
  Short TTL (e.g. 15 min), single-use (set `consumed_at`), rate-limited per
  email.
- The token is delivered two ways in one mail: a **clickable link**
  (`?lt=<token>`, for web) and the **same value as a typeable code** (for VS
  Code / device entry). Verification accepts it regardless of channel.

### B2 — Magic-link login → session

- `POST /api/auth/login/request {email}` (**public path**): look up the user
  by `users.email`; if found, create a `login_tokens(purpose=login)` row and
  email the link+code. **Always return 200** (don't leak whether the email
  exists). Rate-limit.
- `POST /api/auth/login/verify {token, remember: bool}` (**public path**):
  validate the unconsumed, unexpired token; consume it; **mint a `sessions`
  row** and return the session token + `expires_at`. `remember=false` →
  short TTL (e.g. 12 h); `remember=true` → long TTL (e.g. 30 d).
- FE stores the session token per `remember`: **sessionStorage** (ephemeral,
  dies with the tab — the internet-café case) vs **localStorage** (trusted
  device). Default = ephemeral.
- This is the flow that actually solves the foreign-machine concern the C1
  design deferred here.

### B3 — Invites (admin onboards a user) — reuses B1/B2

- `POST /api/invites {email, role, parent_user_id?}` (admin-only): create the
  `users` row (+ `provision_user_defaults`, see B4), then issue a
  `login_tokens(purpose=invite)` and email an onboarding magic-link. Returns
  the link too (so the admin can also hand it over directly / show a QR).
- `POST /api/invites/{token}/accept` (**public path**): validate → mint a
  session (same as verify) → invitee lands logged in. For a `child` whose
  email is a parent's inbox, the parent completes this step.

### B4 — User CRUD, provisioning, roles & enforcement

- `repository/users.py`: `create_user(engine, *, email, role,
  parent_user_id)` → inserts the `users` row (no token; sessions are minted
  at login).
- `provision_user_defaults(engine, user_id)`: seed the per-user default
  persona (reuse C1's `ensure_personas_backfill`, parameterised by
  `user_id`). The "new user gets a working Holzi" path C1 deferred.
- `GET/POST/DELETE /api/users` (admin-only). Deleting a user should remove
  their personal data + all their `sessions`. **Caveat (C1 review):** the
  `user_id` FK `ondelete=CASCADE` is only enforced on **fresh** DBs — SQLite
  `ALTER TABLE ADD COLUMN` can't attach an FK, so on *upgraded* DBs the
  cascade won't fire. So `delete_user` must **explicitly** delete the user's
  conversations/notes/agent_tasks/personas/sessions (don't rely on the DB
  cascade), or the affected tables must be rebuilt with the FK. Either way,
  don't assume cascade.
- Roles `admin`/`member`/`child`; helper `require_role(request, *allowed)`
  → 403. **`child` tool gating**: in the approval path (`routes/api.py`
  `session_approvals` + `tool_approvals`), force approval on **every** tool
  call when `current_role == "child"`, regardless of standing grants.
  (Approval *delivery* — child self-approves in-session vs. a parent
  approves — is an open question; default to in-session self-approval to
  avoid a cross-user approval inbox in C2.)

### B5 — Resource sharing model (schema + read filtering; runtime in C3)

The user chose **per-user + admin sharing**. For the infra tables C1 left
global:
- Add `owner_id INTEGER REFERENCES users(id)` to `llm_credentials`, `skills`,
  `mcp_servers`, `workspaces` (backfill `owner_id = 1`).
- A generic `resource_shares` table: `(resource_type, resource_id,
  shared_with_user_id, shared_with_family BOOL)` — admin shares a
  credential/skill/MCP/workspace with a member or the whole family. (Use an
  explicit `shared_with_family` boolean, not a magic `user_id=0`.)
- Read model `visible_to(user_id)` = `owner_id = U` OR a matching
  `resource_shares` row (incl. family-wide). C2 adds the query + admin UI +
  read filtering; **C3** flips the *runtime* (which credential the agent
  actually uses, per-user MCP). Solves the child/no-key problem: admin shares
  the family LLM credential with everyone, including children.

### B6 — Sessions across frontends + session management

- **Web**: ephemeral by default (B2). "Stay signed in" toggle on the login
  page.
- **VS Code**: same magic-link, *code/deep-link* variant — the webview shows
  an email field → user enters email → enters the code from the mail (or
  clicks the link which deep-links `vscode://<publisher>.holzi/auth?token=…`
  via the extension's URI handler) → `verify` mints a session. Store the
  session token in **VS Code `SecretStorage`** (OS keychain), not plaintext
  settings. The existing extension→webview token `postMessage` bootstrap is
  unchanged in shape.
- **haex-vault (DID, future)**: a DID login *strategy* that verifies a
  verifiable presentation and mints a session — no change to the per-request
  resolver, no schema change. Out of scope for C2 unless haex-vault lands.
- **Session management** UI + endpoints: `GET /api/auth/sessions` (list my
  active sessions: label/user-agent, created, last_used, current?),
  `DELETE /api/auth/sessions/{id}` (revoke one), `POST /api/auth/logout/all`
  (revoke all but current — "log out everywhere"). Add a periodic prune of
  `sessions WHERE expires_at < now` to the existing sweeper.

### B7 — `/settings/family` + login UI (monorepo `~/Projekte/holzi/`)

- Login page: email field + "send me a link" + "stay signed in" toggle; a
  callback route handling `?lt=<token>` → calls `verify` → stores session →
  redirects in.
- `/settings/family` (admin-only; gate via the `isAdmin` flag from C1):
  roster (users + roles), invite (email + role), revoke; sharing UI
  ("share with… member / whole family").
- `/settings/sessions` (any user): list active sessions, revoke, "log out
  everywhere".
- Files: `packages/holzi-ui/pages/settings/family.vue`,
  `pages/settings/sessions.vue`, `pages/login.vue` (rework), composables
  (`useAuth`/`useFamily`), nav (`lib/settingsNav.ts`), i18n (DE/EN — Wave 0).

### B8 — Thread per-user identity into the agent loop + remaining global infra (from the C1 review)

C1 scoped the four personal-data tables but left several multi-user gaps that
are inert under single-admin C1 and become **live cross-user bugs as soon as
a second user exists**. The C1 CodeRabbit review surfaced these; they are
required C2 work:

- **Agent tool-execution loop is identity-blind.** `src/hermes/tools/`
  handlers are hard-coded to `user_id=1` (flagged with TODOs in C1):
  `bootstrap.py` (persona/bootstrap writes), `memory.py` (notes + conversation
  recall; `recall_memory` also mixes message hits with no owner filter),
  `productivity.py` (`task_create`/`task_list`/`task_delete`). The chat route
  knows `current_user_id(request)`, but that identity is **not** carried into
  the tool catalog / agent loop. C2 must thread the acting `user_id` from the
  request → agent run → tool invocation so every tool operates on the caller's
  rows (not the admin's). This is the largest item and a prerequisite for
  letting non-admin users use tools at all.
- **`channel_prompts` is global.** `default_persona_id` lives on a global
  `channel_prompts` row, so a channel→persona pin is last-writer-wins across
  users and can reference a persona the reader doesn't own (the resolver falls
  back safely, but the binding is shared). C2: add `user_id` to
  `channel_prompts` (or a `user_channel_prompts` table) and scope
  `channels_repo.get`/`update` by `current_user_id`.
- **Bootstrap flag is single-user.** `is_bootstrap_completed(engine)` and the
  bootstrap-hint branch in `get_effective_system_prompt` check user 1
  globally; `mark_bootstrap_complete` writes user 1. `users.bootstrap_completed`
  is already per-row — thread `user_id` through the read (the prompt path
  already has it) and the write (rides the tool-loop identity work above) so
  each user gets their own onboarding.

These pair naturally with B3 (roles) and B4 (user provisioning): a freshly
invited user must get their own bootstrap flow, their own task/note/persona
tools, and their own channel pins.

## Open questions to resolve at C2-plan time

- **Monorepo CI gap (process):** `~/Projekte/holzi` has **no GitHub Actions
  workflow**, so the C1 frontend shipped unverified by CI (and its vitest
  `@nuxt/test-utils` env doesn't resolve the `~` alias locally). Add a
  frontend test + typecheck workflow before/with the C2 `/settings/family`
  UI so the FE is actually gated.

- **SMTP transport**: bundled SMTP client to an operator-provided relay
  (Gmail app-password / Mailgun / self-hosted) — pick the config surface.
  Provide a console/no-op transport for dev + tests.
- **Child approval delivery**: self-approve in-session (default) vs. parent
  approves (needs a cross-user approval inbox — heavier).
- **VS Code magic-link UX**: code-entry vs `vscode://` deep-link (or both).
- **Session TTLs**: concrete values for ephemeral vs "stay signed in".
- **Rate-limiting / abuse**: login-request throttling per email/IP.

## Success criteria (from Plan 35 §C2, refined)

- A member enters their email, receives a link, clicks it, and lands logged
  in — **nothing persists** in a foreign browser when "stay signed in" is
  off (session in sessionStorage + short server TTL; closing the tab ends it).
- Admin invites a second user by email; they onboard via the emailed link as
  a `member` with their own empty conversation list + default persona.
- Admin sees the family roster and can revoke a member (all their sessions
  die immediately).
- Admin shares the family LLM credential with a `child`; the child can chat.
- A `child` is prompted for approval on every tool call.
- Two members never see each other's conversations/notes/personas (C1
  guarantees the data scope; add a two-user integration test).
- A user lists their active sessions and can "log out everywhere".
