# Wave C3 — Per-User Runtime Isolation & Resources (OUTLINE)

> **For Claude:** OUTLINE, not a task-by-task plan. Expand into a full TDD
> plan **after C2 lands**. Depends on: **C1 + C2 complete**. This is the
> last Wave-C slice and the one Plan 35 §E (Subagents) waits on.
>
> **Important correction vs. Plan 35:** the roadmap's `app.state` names are
> stale. Verified against the code (`src/hermes/main.py:101-130`,
> `src/hermes/routes/api.py`):
> - `cancel_registry` → really `app.state.chat_runs`, keyed by `run_id`
>   (UUID) → **already isolated**; only needs an ownership check.
> - `approval_futures` → really `app.state.approvals`, keyed by
>   `approval_id` (UUID) → **already isolated**; ownership check only.
> - `sse_buffers` → **do not exist in `app.state`**; SSE queues are
>   per-request (`routes/api.py:395`) → already isolated.
> - `session_approvals` → `dict[conversation_id, set[str]]` → **needs
>   user-awareness** (a conversation is owned by a user post-C1, so this is
>   mostly an ownership check, not a re-key).
> - `sandbox_manager` and `mcp_servers_manager` → the real per-user work.
>
> So C3 is **smaller than the roadmap implies**: the registries are already
> request/run-scoped; the genuine work is (a) ownership checks so user A
> can't touch user B's run/approval, (b) per-user sandbox containers, (c)
> per-user MCP visibility, and (d) wiring C2's sharing model into the
> runtime.

**Goal:** Per-user runtime isolation: User A's sandbox crash, MCP servers,
running chats, and approvals are invisible to and untouchable by User B; the
agent uses the credential/skills/MCP **visible to the calling user** (own +
admin-shared, per C2's sharing model).

## Building blocks

### B1 — Ownership checks on the in-memory registries (LOW effort)

- `chat_runs` / `approvals`: when a request cancels a run
  (`POST /api/chat/runs/{run_id}/cancel`, `routes/api.py:614`) or resolves
  an approval (`routes/api.py:746`), verify the run/approval belongs to the
  caller's `user_id` before acting → else 404. Store `user_id` alongside the
  event/future (small dataclass instead of a bare `asyncio.Event`/`Future`),
  or look up the owning conversation.
- `session_approvals`: a conversation is user-owned after C1; before
  reading/writing `session_approvals[conversation_id]`, confirm the
  conversation belongs to `current_user_id`. (No re-keying needed — the
  conversation ownership *is* the user scope.)
- Tests: user B gets 404 trying to cancel user A's run / resolve user A's
  approval.

### B2 — Per-user sandbox containers (MEDIUM effort)

- `SandboxManager` (`src/hermes/sandbox/manager.py`) keys workspaces by
  `workspace_id` in `_workspaces: dict[str, SandboxHandle]`. Re-key to
  `(user_id, workspace_id)` (or namespace the Podman container/volume name
  with the user: `hermes-ws-{user_id}-{workspace_id}`).
- One container per **active user** (not per session), bounded by a new
  `MAX_ACTIVE_USERS` env setting (`config.py`) — evict the
  least-recently-used user's idle sandbox when over the cap. Document the
  small-host memory cost (Plan 35 §C3 open question) in the config comment.
- Health watcher + crash handler (`add_crash_handler`, `routes/api.py:490`)
  iterate per user; crash events carry `user_id`.
- `sandbox_crashes` table gains `user_id` (the C1 design deferred this
  here). Diagnostics (`routes/diagnostics.py`) filter crashes by
  `current_user_id` → "User A's crash doesn't surface to User B."
- Workspaces become user-scoped/shared per C2's `owner_id` + shares.

### B3 — Per-user MCP visibility (MEDIUM-HIGH effort)

- `mcp_servers_manager` (`src/hermes/mcp_manager.py`) holds all enabled
  servers in `_handles: dict[int, McpServerHandle]` globally and feeds
  `app.state.tool_catalog`.
- Decision point: do we run a **per-user manager** (`dict[user_id, ...]`,
  true process isolation, higher memory) or keep **one manager but filter
  the tool catalog per user** at request time using C2's `visible_to(user_id)`
  read model (cheaper, sufficient for a family box)?
  **Lean: filter at request time.** The agent's tool catalog for a turn is
  assembled from built-ins + the MCP servers visible to that user. A family
  member cannot see another member's private GitHub MCP because it's not in
  their visible set. Only promote to per-user managers if isolation of the
  *connection* (not just visibility) is required.
- The inbound `app.state.mcp_manager` (the `/mcp` StreamableHTTP server that
  exposes Hermes to Cline) is a separate concern — scope its exposed catalog
  to the authenticated user too.

### B4 — Scheduler & sweeper scoping (LOW effort)

- `AgentTaskScheduler` stays global but already fires per-task with
  `user_id` after C1/C2 — verify fired runs + created conversations carry
  the task's `user_id`.
- `ConversationSweepScheduler` is global TTL cleanup — it deletes expired
  rows across all users by design; confirm it respects per-user bookmarks
  (it already filters `expires_at IS NOT NULL`, which is per-row, so no
  change). Document that the sweep is intentionally cross-user.

### B5 — Wire C2 sharing into the runtime credential path

- `resolve_persona_context` / `build_client_for_credential`
  (`routes/api.py`, `src/hermes/personas.py`) pick the *active*
  `llm_credentials` row. Post-C2 there can be a shared family credential.
  Resolve the credential from the set **visible to the calling user** (own
  active, else the family-shared active). This is what lets a `child` chat
  using the admin's shared key.

## Success criteria (Plan 35 §C3)

- Two browsers as two members each see their own conversations, personas,
  notes (C1 already guarantees data; C3 adds runtime: sandbox, MCP, runs).
- Admin sees the family roster + can revoke (C2) — unchanged.
- A sandbox crash for User A does not appear in User B's diagnostics.
- User B cannot cancel User A's chat run or resolve User A's approval.
- A child using the admin-shared credential can run the agent end-to-end.
- `MAX_ACTIVE_USERS` bounds sandbox memory on a small host; eviction works.
- Full backend suite + `mypy src` green; an integration test spans two
  concurrent users with separate sandboxes.

## Effort summary (verified, not from the roadmap)

| Block | Effort | Note |
|---|---|---|
| B1 ownership checks | LOW | registries already UUID/conversation-keyed |
| B2 per-user sandbox | MEDIUM | re-key `_workspaces`, namespace containers, LRU cap |
| B3 per-user MCP | MEDIUM-HIGH | prefer catalog filtering over per-user managers |
| B4 scheduler/sweeper | LOW | mostly verification post-C1/C2 |
| B5 shared credential runtime | LOW-MEDIUM | resolve credential from visible set |
