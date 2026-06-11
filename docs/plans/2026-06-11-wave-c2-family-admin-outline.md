# Wave C2 — Email Magic-Link Login, Invites, Roles & Sharing (OUTLINE)

> **For Claude:** OUTLINE, not a task-by-task plan. Firm this up into a full
> TDD plan **after C1 has landed** — decisions here depend on C1's final
> shapes (the `IdentityResolver`, the `sessions` table, how `user_id` is
> threaded, the per-user default-persona seeding). Re-read the C1 design doc
> + Plan 35 §C2 before expanding.
>
> Depends on: **C1 complete** (`2026-06-11-wave-c1-multi-user-impl.md`).
> Blocks: **C3** (per-user runtime isolation).

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
- `GET/POST/DELETE /api/users` (admin-only). Deleting a user cascades their
  personal data **and all their `sessions`** (FK `ondelete=CASCADE` from C1)
  → every session of that user dies immediately (revoke).
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

## Open questions to resolve at C2-plan time

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
