# Design: Holzi → Multi-Tenant Coding-Agent SaaS

> **For Claude:** DESIGN DOC, brainstormed 2026-06-11. Supersedes the
> family-box/SQLite assumption of the Wave-C docs for the *primary* target.
> Built section by section with the user; each section is validated before the
> next. Status per section marked below.

## §0 — Baseline & Scope (locked)

Holzi pivots from a single-box family assistant to a **multi-tenant, coding-focused
SaaS**, all-Postgres, with hard per-user isolation. We keep the existing custom
`hermes-server` backend as the base (it is *not*, and never was, built on
`NousResearch/hermes-agent` — that is a separate single-user local agent we use
only as an MIT *reference/parts donor*).

**Locked decisions:**

| Area | Decision |
|---|---|
| Base | Keep custom `hermes-server` (FastAPI, SQLAlchemy Core async). Drop SQLite/family-box as primary. |
| DB | **Postgres-only**, hard isolation via **Row-Level Security** (RLS), reusing the Specifyr RLS pattern. |
| LLM plane | **LiteLLM** (per-user virtual keys, budgets, spend, routing) as the front; **haex-claude-proxy** behind it for the **subscription/OAuth** path LiteLLM can't do. |
| Memory (Layer B) | **Adopt hindsight** behind a `MemoryProvider` seam; per-tenant schema + per-user bank isolation. |
| Learning loop | **Port from `NousResearch/hermes-agent`** (MIT, ~4.5k lines): background-review skill induction, counter-based self-nudge, `skill_manage`, `session_search`, `trajectory_compressor`. |
| Extensibility | Per-user **Skills + MCP servers**, sandboxed (Podman, already in the repo). |
| Code knowledge (Layer A) | **Optional/toggle** feature (tree-sitter index), not foundational. Validate value via graphify's MCP first; build native if it proves out. |
| Audit | **Every** platform/org admin config change recorded in an `audit_log` (actor, action, scope, before→after, ts). |
| Rejected | caveman (goal met by trajectory-compressor + persona verbosity), goose (whole agent), agentgateway (deferred to per-user MCP governance). |

## Section outline

- **§1 — Foundation: Postgres + RLS multi-tenant isolation** ← *locked*
- **§2 — Identity, Roles & Organizations** ← *locked*
- **§3 — LLM plane: LiteLLM + Claude-proxy + org governance** ← *locked*
- **§4 — Memory: hindsight behind a `MemoryProvider` seam (Layer B)** ← *locked*
- **§5 — Learning loop ported from hermes-agent** ← *locked*
- **§6 — Extensibility: per-user Skills + MCP (sandboxed)** ← *locked*
- **§7 — Layer A: optional code index (tree-sitter)** ← *locked*
- **§8 — Migration & rollout (greenfield, sequencing)** ← *locked*

---

## §1 — Foundation: Postgres + RLS multi-tenant isolation  *(locked)*

**Why first.** Every other subsystem (memory, loop, skills, MCP, billing) reads
and writes per-user data. Hard isolation must be a property of the **data layer
itself**, not of app-level `WHERE user_id = …` clauses that a future bug could
forget.

**Tenant model.** The hard isolation boundary is the **user**. `user_id` is the
partition key on every personal-data table (`conversations`, `messages`,
`notes`, `agent_tasks`, `personas`). A second **`org_id`** dimension sits *above*
users for *shared* resources (`skills`, `mcp_servers`) — designed in §2 — and
never weakens the per-user floor on personal data.

**Isolation mechanism — Postgres RLS (defense-in-depth).** Wave C already
threads `user_id` and filters in the repo layer. We escalate that to
DB-enforced **Row-Level Security**: each personal table gets
`CREATE POLICY ... USING (user_id = current_setting('app.user_id')::bigint)`.
The app connects as a **non-owner role without `BYPASSRLS`**, so even a query
that forgets its `WHERE` returns zero foreign rows. This is the Specifyr RLS
pattern, reused.

**Per-request context.** The `IdentityResolver` (Wave C) resolves the session
bearer → `(user_id, role)`. The DB layer then issues
`SET LOCAL app.user_id = $1` at transaction start (per connection checkout), so
RLS sees the authenticated user. `/ws/agent` uses the same resolve path.

**Storage (greenfield Postgres).** SQLAlchemy Core stays (mostly dialect-agnostic);
driver `aiosqlite → asyncpg`. SQLite-only pieces are replaced: **FTS5 → Postgres
`tsvector` + GIN** (for the ported `session_search` and any app-side text search);
the PRAGMA-guarded `_apply_lightweight_migrations` → **Alembic**. **No data
migration / no backward-compat** (nothing productive yet — §8): the schema is built
fresh and the **platform_admin is seeded from env at boot** (`HERMES_PLATFORM_ADMIN_EMAIL`, §2).

**hindsight alignment.** App-data isolation = RLS; *memory* isolation =
hindsight's own tenant-schema + per-user-bank model. Two mechanisms, one
per-user boundary — they don't fight.

**Decisions (resolved 2026-06-11):**
1. **Alembic** adopted for migrations (real SaaS migrations).
2. **Tenant = user** is the hard floor for *personal* data. Shared resources add
   a second `org_id` dimension (§2). A user belongs to **≤ 1 org**.
3. RLS context via **`SET LOCAL app.user_id` (+ `app.org_id`)** per transaction.

---

## §2 — Identity, Roles & Organizations  *(locked)*

Extends §1. Personal data stays hard per-user; this section adds the **role
hierarchy**, the **organization** layer for *shared* resources, and the
**registration/join policies** that gate who gets in.

**Roles (3 tiers):**
- **`platform_admin`** — **first one seeded from env at startup**
  (`HERMES_PLATFORM_ADMIN_EMAIL`), *not* "whoever registers first" (a land-grab
  risk). Configures platform-wide policy: registration mode, allowed email
  domains, whether users may self-create orgs, and the platform-wide model
  allowlist + quota caps (§3). **Multiple allowed** — see Admin management.
- **`org_admin`** — a user who created an organization, plus any member promoted
  to org_admin. Full rights over *that* org: join policy, member management,
  org-shared skills/MCPs. **Multiple allowed** — see Admin management.
- **`member`** — a normal user. Belongs to **≤ 1 org** or none (solo).

**Admin management (promotion).** Both admin tiers are **sets, not singletons**:
- A **platform_admin** can promote any user to `platform_admin` and demote back to
  member. A platform_admin is **never in an org** (`org_id IS NULL`, DB CHECK —
  *separation of concerns*); promoting to platform_admin clears org membership.
- An **org_admin** can promote/demote **members of their own org only**.
- **Lock-out guard:** always keep **≥ 1 platform_admin**, and an org with members
  **≥ 1 org_admin** — the last admin can't be demoted/removed without first
  transferring the role.
- Promotions/demotions are recorded in the **audit log** (below), like all admin actions.

**Audit log (cross-cutting).** **Every** platform- or org-admin configuration change
is recorded — not just promotions: registration/join policy, model allowlist +
quotas (§3), resource caps + egress denylist (§6), skill/MCP approvals +
provisioning (§6), member role changes. An **`audit_log`** table:
`actor_user_id, scope (platform | org:{id}), action, target_type, target_id,
before (jsonb), after (jsonb), created_at`. RLS: platform_admins see all;
org_admins see **their org's** entries; members see none.

**Bootstrap (startup seeding).** The platform_admin is **created at boot from
env**, idempotently (extends today's `ensure_users_seeded`):
- **`HERMES_PLATFORM_ADMIN_EMAIL`** — seeds a `users` row with `role=platform_admin`.
- **`HERMES_PLATFORM_ADMIN_TOKEN`** — seeds a **long-lived session** for that user,
  stored **hashed** (§1's session model); the operator authenticates with this
  token directly — **no SMTP/magic-link needed for the admin**.

This is essentially today's static `HERMES_AUTH_TOKEN` (static bearer → admin),
evolved: now bound to an explicit admin email + the `platform_admin` role.
Magic-link login is for **self-service users** (org admins / members who sign up);
the env-configured operator is the one exception. *(If `…_TOKEN` is omitted, the
admin would have to use magic-link once SMTP exists — but setting it is the
expected path.)*

**Organization model:**
- New `orgs` table: `id, name, created_by, join_mode, allowed_domains, created_at`.
- `users.org_id` — **nullable FK, at most one org per user** → a single column,
  no join table (YAGNI). `org_id IS NULL` ⇒ solo user.
- Creating an org sets the creator's `org_id` and `role = org_admin`.
- The org model **subsumes family-mode**: a family is just an org; the former
  `child` role becomes an optional restricted-member flag. One concept, not two.

**Registration (platform) & join (org) policies:**
- Platform singleton: `registration_mode ∈ {open, invite_only, domain_allowlist}`,
  `allowed_domains[]`, `allow_org_creation` (bool).
- Per-org: `join_mode ∈ {invite_only, domain_allowlist}`, `allowed_domains[]`.
- `invitations` table: `email, org_id (NULL = platform-level), role, token_hash,
  expires_at, invited_by`. Accepting an invite mints a session via the Wave-C2
  magic-link minter — self-service login is uniformly magic-link (the
  env-configured platform_admin is the one exception — see Bootstrap).

**Resource sharing & visibility (extends RLS):**
- **Personal data** (`conversations`, `notes`, …) — always per-user; org
  membership never exposes another member's conversations.
- **Shared resources** (`skills`, `mcp_servers`) carry `owner_user_id`, `org_id`
  and a `scope`/`status`:
  - **private** (default when added): only the owner; `org_id` NULL.
  - **published-to-org**: owner *requests* publish → `status=pending_review` →
    an **org_admin approves** → `status=approved`, visible to all org members
    (reject → back to private).
  - **org-provided**: an org_admin offers it directly at org level →
    `status=approved` immediately, available to every member from the start.
  RLS: `USING (owner_user_id = app.user_id OR (org_id = app.org_id AND status='approved'))`;
  `pending_review` rows are additionally visible to the org's admins for review.
  *(Publish/approval + org-provisioning workflow = §6.)* **Two scopes only —
  private + org; no platform-global.**
- The resolver therefore yields `(user_id, role, org_id)`; the DB sets
  `SET LOCAL app.user_id` **and** `app.org_id` per transaction (extends §1).

**Data sensitivity classes (refined 2026-06-11):**
- **Strictly per-user — never shareable, RLS-isolated, secrets encrypted at
  rest:** `sessions`, `llm_credentials` + subscription/OAuth tokens,
  `conversations`, `messages`, `notes`, `agent_tasks`, `personas`. (Moves
  `llm_credentials` out of Wave C's tentative "shareable infra" — credentials
  and subscriptions are personal, full stop.)
- **Per-user by default, org-shareable via governance:** `skills`,
  `mcp_servers` — private when added; reach org scope only via owner-publish +
  **org_admin approval**, or by org_admin **direct provisioning**. (Flow: §6.)

**Decisions (resolved 2026-06-11):**
1. Any user may self-create an org, gated by platform `allow_org_creation` = **yes**.
2. **Two** visibility tiers only — **private + org** (no platform-global).
3. Org **subsumes family-mode** — one concept, not two.
4. On leave/switch: org-shared resources the user created **stay with the org**; personal data stays with the user.
5. Skills/MCPs reach org scope two ways: **owner-publish → org_admin approval**, or **org_admin direct provisioning** (instant for all members). Detailed flow in §6.
6. **Multiple admins per tier:** platform_admins + org_admins promote/demote within scope (org_admins: **own-org members only**); a **platform_admin is never in an org** (`org_id IS NULL`, DB CHECK — separation of concerns); **last-admin lock-out guard** + audit apply.
7. **Audit log:** every platform/org admin config change (policies, models, quotas, caps, approvals, role changes) is recorded with actor + scope + before→after + timestamp; org_admins see their org's log, platform_admins see all.

---

## §3 — LLM plane: LiteLLM + Claude-proxy + org governance  *(locked)*

**Topology (locked in §0).** **LiteLLM Proxy** is the front — per-user virtual
keys, budgets, spend, provider routing. **haex-claude-proxy** sits *behind*
LiteLLM as an Anthropic-compatible backend for the **subscription/OAuth** path
LiteLLM cannot do. API-key providers (OpenAI, OpenRouter, pay-as-you-go
Anthropic) → LiteLLM direct.

**Identity → key mapping.** Holzi's `(user_id, role, org_id)` maps onto
LiteLLM's `Org → Team → User → Key` hierarchy: Holzi **org → LiteLLM team**,
Holzi **user → LiteLLM key**. The resolver hands the agent the caller's virtual
key; every model call carries it, so allowlist + budgets + spend are enforced at
the gateway, not in app code.

**Org-level governance (refined 2026-06-11).** Three *orthogonal* axes per model
— **permit ≠ fund ≠ credential**:

1. **Permit (compliance):** is the model allowed? Effective allowlist =
   `platform_allowed ∩ org_allowed` → LiteLLM team model-access list. A
   not-permitted model is blocked regardless of credentials.
2. **Fund (optional):** the org may *allocate* a **monthly, per-user, per-model
   budget** (org/platform key, metered) → LiteLLM per-model/per-user
   `model_max_budget` + TPM/RPM. *Additive* — funds API usage on the org's dime
   without the member bringing a key. **Never funds a subscription** (those stay
   strictly BYO). *(Verify exact LiteLLM surface at impl.)*
3. **Credential (per-user, NEVER org-shared):** the member may instead use their
   **own** API key or their **own Claude subscription** (OAuth via
   haex-claude-proxy). **A subscription is strictly individual — the org never
   holds or shares one.**

**Access resolution** for a call (user `u`, model `m`): `m` permitted? → no ⇒
blocked. → yes ⇒ choose credential: the user's own key/subscription if connected
(their own cost/quota); else, if the org funded `m` for `u`, the org/platform
key (metered to the org budget); else ⇒ "connect a credential" prompt.

**Subscription example (the user's case):** org allows Claude / Claude-Code but
does *not* (cannot) fund a subscription → the member **logs in with their own
Claude subscription** to use it. **Org allowance = permission, not provisioning.**

Precedence: **platform → org → user**. A solo user (no org) is governed by
platform defaults + their own credential/budget.

**Credential model — the open fork.** Whose provider credentials power the calls?
- **(a) Platform-provided, metered:** platform holds provider keys; LiteLLM
  meters per-user spend → platform bills users. Simplest UX; platform carries
  cost + risk.
- **(b) BYOK / BYO-subscription:** each user (or org) brings their own API key or
  their own Claude subscription (OAuth via haex-claude-proxy's per-user
  resolver). Platform fronts no cost; org governance still applies on top.
- **(c) Both:** platform-keys as default, BYOK as opt-in.

**Subscription-in-multi-tenant constraint (important).** One Claude Max
subscription **cannot** legitimately serve many external customers (Anthropic
ToS = individual use; rate limits). So the subscription path realistically =
the **operator's own usage** + **BYO-subscription per user** (each user's own
Claude account via OAuth) — *not* a shared platform subscription. This is why
haex-claude-proxy's per-user credential resolution matters.

**Decisions (resolved 2026-06-11):**
1. Credential model = **(c) both** — BYOK/BYO-subscription is the default; optional
   metered platform-keys. Org governance applies on top regardless of source.
2. Budget units: support **both** — USD (API keys) + token/request quotas (subscription).
3. Mapping confirmed: **org → LiteLLM team, user → LiteLLM key**.

---

## §4 — Memory: hindsight behind a `MemoryProvider` seam (Layer B)  *(locked)*

**Layer B = experiential memory** (preferences, project conventions, decisions,
recurring patterns) — distinct from per-turn conversation history (app Postgres)
and from code structure (Layer A, §7). We **adopt hindsight**, but behind a
`MemoryProvider` seam (mirroring hermes-agent's `agent/memory_provider.py`) so it
is swappable (hindsight / Honcho / Mem0 / a null-impl for tests) and never leaks
into route/agent code.

**Interface** (`hermes/memory/provider.py`, Protocol):
`initialize · prefetch · recall(query) · retain(items) · reflect(query) ·
sync_turn · shutdown`. Default impl wraps the hindsight REST/SDK; a `NullProvider`
(FTS-only) keeps tests and a minimal deploy working.

**Isolation.** hindsight memory is **per-user**: `bank_id = "user:{user_id}"`,
**derived server-side from the authenticated session — never from user input**
(same discipline as the RLS context). The Holzi backend is hindsight's only
client; users never address it directly. That server-derived bank is the
isolation floor. Optional escalation for org customers demanding a DB boundary:
hindsight **tenant = org** (Postgres-schema-per-org); solo users share a `solo`
tenant, isolated by bank.

**Retrieval & injection.** At conversation start the provider `prefetch`es a
curated *"what we know about this user / this project"* block (hindsight Reflect
+ Mental Models) → injected into the system prompt. This fixes today's gap where
`recall_memory` only fires if the model remembers to call it. On-demand `recall`
stays available as a tool.

**Granularity.** One bank per user; **project/workspace is a tag** on memories
(hindsight tags/fact_types), not a separate bank — avoids bank explosion while
letting recall filter to the current project.

**Cost path.** Every `retain`/`reflect` is LLM work → routed through LiteLLM, so
it counts against budgets. Default: charged to the **user's** budget (it is their
data); a separate platform "memory budget" is possible.

**Feeds from the loop.** §5's auto-Retain reads finished conversations (app
Postgres) and calls `retain`; the periodic Reflect updates Mental Models. §4 is
the substrate; §5 drives it.

**Decisions (resolved 2026-06-11):**
1. Isolation: server-derived `bank = user:{id}` is the floor; schema-per-org only on demand.
2. One bank per user; **project = tag** (not a separate bank).
3. Memory-op LLM cost charged to the **user's** budget; platform memory-budget optional.
4. hindsight = co-located Docker service, **same Postgres server, dedicated `hindsight` database** (not a separate server, not co-mingled).

---

## §5 — Learning loop (ported from hermes-agent)  *(locked)*

The loop is what makes Hermes "do more than random notes": it **learns from each
conversation** — distilling durable memory and inducing/refining reusable
**skills** — without the user asking. Ported from hermes-agent (MIT), adapted
from its local single-process design to Holzi's multi-tenant server.

**Shape: a constrained background "review" run.** After every *N* turns (and at
session end), Holzi enqueues a **review job** that runs a constrained agent over
the just-finished transcript, under the **user's identity + budget**, with a
tool whitelist limited to `skill_manage` + memory `retain`/`reflect`. It:
1. **Retains** durable facts/preferences/decisions → hindsight (§4 provider).
2. **Induces/refines skills:** detects recurring techniques or user corrections
   → `skill_manage(create | edit | patch)` into the user's `skills` rows.

This is hermes-agent's `background_review.py` pattern, but as a **decoupled
background job** (off the request path) instead of a forked daemon thread.

**When is a "session end"? (finalization model).** A persistent chat server has
no single "end" event, so the loop is **delta-based with a per-conversation
watermark** (`last_reviewed_message_id`): a review only ever processes messages
added since the last review. Finalization is *triggered* by, whichever comes first:
- **Idle:** no new message for `T` minutes (default ~15–20, configurable) — the
  universal trigger across web / Signal / VS Code.
- **Explicit close:** the `/ws/agent` WebSocket disconnects (VS Code extension
  closes) or the user archives the conversation — an early idle-flush.
- **Every N new turns:** long sessions are reviewed mid-flight so learning isn't
  lost if they never cleanly "end."
- **Pre-TTL safety net:** before a conversation is TTL-pruned, review its
  unreviewed tail (closes today's "delete without consolidation" gap).

The watermark makes all triggers **idempotent and resume-safe**: a premature
review processes a partial delta; the next trigger handles the rest. So no precise
"end" is needed — idle-flush + N-turns over the delta covers every channel.

**Adaptations (local → server):**

| hermes-agent | Holzi |
|---|---|
| forked daemon thread | background job (scheduler / task queue), per-conversation |
| skills as `~/.hermes/skills/*.md` | `skills` **table** (per-user, RLS, §2 share model); content stays YAML-frontmatter markdown |
| `MEMORY.md` / `USER.md` | hindsight Mental Models via `MemoryProvider` (§4) |
| `session_search` FTS5 (SQLite) | `session_search` over Postgres **tsvector**, per-user |
| counter-based nudge flags | per-conversation counters → enqueue review |
| `trajectory_compressor.py` | ported for **context compaction** — the real coding token-lever (replaces caveman) |

**Injection at conversation start:** prefetch the user's skills (catalog +
`when_to_use`) and the hindsight "what we know" block → system prompt. The
skills-catalog mechanism already exists; we add the memory block.

**Autonomy:** private skill creation/refinement is **autonomous** (they are the
user's own private skills). Publishing a skill to the org still requires
**org_admin approval** (§2) — the loop never shares without governance.

**Lift vs. rebuild:** lift the *prompts + decision logic* of
`background_review.py` and `trajectory_compressor.py` (battle-tested, MIT);
reimplement the *runner* for the async multi-tenant server.

**Decisions (resolved 2026-06-11):**
1. Review execution: a **Postgres-backed async task queue** (`procrastinate` /
   `pgqueuer` — `SKIP LOCKED` + `LISTEN/NOTIFY`) behind an `enqueue_review()`
   seam → **no new datastore** (already all-Postgres). A RESP store is added
   **only if** later needed (LiteLLM multi-instance rate-limiting, heavy
   caching) — and then **Valkey** (BSD, drop-in), *not* Redis (license) and not
   Dragonfly unless extreme single-node throughput is required (BSL caveat).
2. Cadence: end-of-session **and** every-N-turns for long sessions, configurable.
3. Loop per user: on by default, with an **off switch + cadence control** (it spends their budget).

---

## §6 — Extensibility: per-user Skills + MCP servers (sandboxed)  *(locked)*

Holzi already has `skills`, `mcp_servers`, `tool_approvals`, `sandbox_crashes`,
`workspaces` and rootless **Podman** integration — today global/single-user. §6
scopes them **per-user** (RLS, §1), adds the **publish/approval** governance
(§2), and **hardens runtime isolation** (Wave C3, made real).

**Adding an MCP server — two kinds, two threat models:**
- **stdio / local** (`uvx …`, `npx …`): user-supplied **code Holzi must run** →
  executed only inside that user's **Podman sandbox** — no host FS (only a scoped
  workspace), egress allowlist, CPU/mem/time limits, no platform secrets, no other
  users' data. The dangerous case; the sandbox is the gate.
- **remote URL** (HTTP/SSE/Streamable): no code on the host, but data leaves to a
  third party + **SSRF risk**. Outbound calls go through an **egress proxy** with a
  **two-level denylist** (*not* an allowlist — an allowlist would break web
  research): a **platform denylist** that *always* applies (includes internal IP
  ranges = anti-SSRF) + an **org denylist** for that org's members. Secrets
  injected per-call, never exposed to the agent.

**Per-user runtime isolation.** Each user's agent run executes its tools
(terminal, file, MCP) inside **that user's sandbox** — including org-shared stdio
MCPs (they run in *each member's* sandbox, never a shared one, so isolation
holds). Crashes recorded in `sandbox_crashes`.

**Resource governance (compute caps).** Two levels, parallel to §3's LLM budgets:
- **platform_admin** assigns each **org** (and each solo user) a cap of
  **CPU / RAM / storage** — the total compute envelope.
- **org_admin** distributes that cap across the org's members/containers and picks
  the **idle policy**: *kill-on-idle* (hardware-saving) or *keep-warm* (faster
  start). So the sandbox lifecycle is a **per-org setting**, not a global one.
Sandboxes are scheduled within these caps; exceeding a cap queues or throttles.

**Skills.** A skill = YAML-frontmatter markdown + optional `references/`,
`templates/`, `scripts/`. The markdown is *instructions* (not executed); any
`scripts/` execute in the user's sandbox. The risk is a malicious *shared* skill
instructing the agent badly → mitigated by the approval gate.

**Publish / approval flow (from §2, detailed):**
- Private (default) → owner requests **publish** → `status=pending_review`.
- The **org_admin review queue** shows the skill content / MCP config (command,
  URL, declared egress, whether it ships executable scripts) → **approve**
  (`approved`, org-wide) or **reject**. This human gate is the security boundary
  for shared extensions.
- **Org-provided:** org_admin creates directly → `approved`. Org-provided MCP
  credentials may be org-managed (shared) **or** flagged "requires user cred".
- Audit: who published, who approved, when.

**Runtime tool approval — who approves?** Default is **self-approval**: the
*acting user* confirms a risky tool before it runs (standard human-in-the-loop;
this is what `tool_approvals` records). Live *third-party* approval (an admin/parent
OKs each action in real time) is impractical — admins aren't always online. So a
**restricted member** is instead governed by **pre-authorization**: the org_admin
(or parent) pre-defines the member's allowed tools / skills / MCPs / models (§3) /
egress, and the member operates freely *within* that policy; anything outside is
**blocked, not queued**. *(Optional later: async delegated approval — the action is
parked and the admin notified — for the rare case that truly needs it.)*

**What actually restricts a member — the boundary is the *tool* layer, not skills.**
A skill is *instructions*: it grants no capability the agent doesn't already have
via its tools (its only executable part, an optional `scripts/`, still runs
through a normal tool). An **MCP**, by contrast, *adds* tools — a **stdio MCP is
code execution**; a remote MCP can reach endpoints the egress denylist would
block. So:
- The real capability boundary = **base-tool whitelist + egress denylist +
  sandbox**. Whatever isn't a whitelisted tool can't run — *regardless of whether
  a skill, an MCP, or the user asked for it*.
- A **restricted member** = a **tool whitelist** (only specific tools) + **no
  MCP-adding** (that would inject new capability-granting tools — e.g. a stdio MCP
  recovering a "terminal" they were denied) + egress denylist.
- Restricting **skill-adding** is *not* a security control (skills can't exceed
  the whitelisted tools) — at most a product/simplicity choice. Malicious *shared*
  skills are handled by the §2 org-publish approval, not by blocking skill-adding.

**Decisions (resolved 2026-06-11):**
1. **Resource caps** set by platform_admin per org/solo-user (CPU/RAM/storage);
   **org_admin** distributes them + picks the **idle policy** (kill-on-idle vs
   keep-warm). Sandbox lifecycle is a per-org setting, not global.
2. Egress = **two-level denylist** (no allowlist): platform denylist *always*
   (incl. anti-SSRF internal ranges) + org denylist for org members.
3. Org-provided MCP creds: **both** — org-managed-shared *or* "requires user cred".
4. Tool approval = **self-approval** by the acting user (live third-party approval
   is impractical). A **restricted member** = a **tool whitelist** + **no
   MCP-adding** (MCPs/stdio = capability escalation past the whitelist) + egress
   denylist; the boundary is the *tool layer*, so restricting *skill*-adding is
   optional (skills grant no capability). Async delegated approval = optional later.

---

## §7 — Layer A: optional code index (tree-sitter)  *(locked)*

**Per-workspace, toggleable — first-class for large/multi-repo codebases**, not a
nice-to-have (corrected 2026-06-11: the loop does **not** make it taper). Gives
the coding agent a structural map (definitions, deps, call-sites, **and cross-repo
links**) it queries *fresh* instead of burning input tokens on grep/read. Reasoning:
- **Layer A (structure) and Layer B (memory) are complementary, not substitutes.**
  Memory captures *judgment / conventions / why*; it is **not** a current, complete
  structural map, and memory fragments go **stale**. A graph rebuilt on code change
  is current.
- **Every session is effectively a cold start** → pulling structure *on demand*
  from a current graph beats re-reading files or relying on partial/stale memory.
  For **large, multi-repo** code (cross-repo deps) the graph's value only grows.
- For **small/greenfield** workspaces a graph is overkill — just read the files.
  → the toggle is about **workspace scale/type**, not "the loop will replace it."

**Approach** — behind a `CodeIndex` seam (like `MemoryProvider`):
1. **Validate + reference:** use **graphify's MCP server** in a single-tenant dev
   setting to confirm the payoff and learn *which* nodes/edges matter (tree-sitter
   parsing, relationship types).
2. **Build native** on **tree-sitter** for per-tenant indexing + security.
3. **Storage must scale — not in-memory.** graphify's ~512 MiB cap
   (`GRAPHIFY_MAX_GRAPH_BYTES`) is a symptom of its **in-memory NetworkX** design:
   fine for a CLI on one repo, wrong for a SaaS holding many large per-tenant
   graphs in RAM. Back the index with a **scalable, disk-backed graph store**.
   - **Apache AGE** (openCypher graph queries *inside Postgres*) — **recommended,
     primarily for isolation consistency**: it inherits Postgres **RLS**, so the
     graph gets the *same DB-enforced per-tenant isolation* as everything else
     (§1). No extra datastore is a bonus, not the main reason.
   - **Neo4j** — license nuance: **Community = GPLv3 (OSS)** but **no multi-database
     / no RBAC** → tenant isolation would be **app-level only**, against our hard-
     isolation line. DB-enforced isolation needs **Enterprise (commercial,
     AGPL dropped ~2018)**. So Neo4j is the **scale-out option later** if AGE hits
     a perf ceiling — at the cost of a commercial license + a second isolation
     paradigm.
   - *Relational adjacency + recursive CTEs* = simplest baseline (no graph engine).
   *(Verify AGE↔PG version compat at impl.)*

**Scope & isolation.** The index is built from the user's repo **inside their
sandbox** (§6) and stored RLS-scoped to the `workspace` (per-user, or org-shared
like other resources). It is a **cache** rebuilt on code change — never the
source of truth.

**Sequencing.** Ships **after** §1–§6. Feature-flagged; off by default.

**Decisions (resolved 2026-06-11):**
1. graphify = validation + parsing reference only; native runtime built on tree-sitter.
2. Graph backend = **Apache AGE in Postgres** (inherits RLS → isolation-consistent);
   Neo4j Enterprise is the later scale-out option; relational+CTE the simplest fallback.
3. Toggle **per-workspace** — on for large/multi-repo, off for greenfield.

---

## §8 — Migration & rollout  *(locked)*

**Relationship to Wave C.** This doc **supersedes the SQLite/family-box
assumption** of the Wave-C plans. Wave C1's *structural* work is **reused on
Postgres** (sessions, `IdentityResolver`, `user_id` scoping, RLS-readiness); its
roles map forward — **family → org**, `admin/member/child` →
`platform_admin / org_admin / member (+ restricted flag)`. The Wave-C docs get a
"superseded by 2026-06-11-saas-coding-agent-design.md" header; their structural
designs live on, their SQLite framing does not.

**Build sequencing (dependency spine).**
1. **§1 Foundation** — Postgres + Alembic + RLS + data migration. *(hard to
   reverse; do first, carefully)*
2. **§2 Identity** — sessions, resolver, orgs, registration/invitations, sharing
   + RLS policies for shared resources.
3. **§3 LLM plane** ∥ **§4 Memory** — LiteLLM + proxy + governance; hindsight +
   `MemoryProvider`. (Independent → parallelizable.)
4. **§5 Loop** — `procrastinate` queue, review job, `skill_manage`,
   `session_search` (tsvector), `trajectory_compressor`.
5. **§6 Extensibility hardening** — per-user MCP/skills, sandbox isolation,
   publish/approval, resource caps, egress denylist.
6. **§7 Layer A** — optional code index, feature-flagged, last.

**Clean start — no data migration, no backward compatibility** (nothing productive
yet, per decision 2026-06-11). Greenfield Postgres:
- Stand up Postgres (one server: `holzi` + `hindsight` DBs).
- Build the schema **fresh** — no SQLite→Postgres data port, no additive-ALTER
  caution, no legacy bootstrap. SQLAlchemy Core stays; **FTS5 → tsvector**;
  **Alembic** is the migration baseline going forward.
- The **platform_admin is seeded from env at boot** (`HERMES_PLATFORM_ADMIN_EMAIL`,
  §2) — no manual registration, no land-grab, no data preservation needed.
- RLS smoke test (cross-user denial) before opening to a second user.

**Rollout phasing (go-to-market).**
- **Phase 0 — dogfood:** stand up the new Postgres stack and use it *yourself*
  (you = the env-configured platform_admin, BYO-subscription). Validates §1–§5 end to
  end with zero multi-tenant complexity. *(Fresh start — prior SQLite data is not
  carried over.)*
- **Phase 1 — invite-only multi-user:** orgs + magic-link sessions; a few trusted
  users by invite (`registration_mode = invite_only`). Exercise isolation (RLS,
  sandbox, per-user MCP) with real second users.
- **Phase 2 — controlled registration:** open `registration_mode`
  (domain_allowlist / open), org self-creation, metering/billing if platform-keys
  are offered. Layer A + agentgateway-for-MCP-governance arrive here as enhancements.

**Risk posture.** With no data migration and no legacy to preserve, there is **no
hard-to-reverse step** — the whole build is greenfield, **additive and flag-gated**,
shippable incrementally.

**Decision (resolved 2026-06-11):** **Clean greenfield Postgres — no data
migration, no backward compatibility** (nothing productive to preserve);
platform_admin is **seeded from env at boot** (`HERMES_PLATFORM_ADMIN_EMAIL`, §2).

**Decisions (resolved 2026-06-11):**
1. Wave-C docs marked **superseded**; structural designs rebased to Postgres; family→org.
2. Phasing: **dogfood → invite-only → controlled registration**.
